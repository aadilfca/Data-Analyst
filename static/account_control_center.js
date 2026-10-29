// Simplified Account Control Centre: Verification and Financial Statement mappings.
let accClientId = null;
let accAccounts = [];
let accLatestUpload = null;
let accSelectedUploadId = null;
let accAvailableUploads = [];
let accTrialBalanceCache = { clientId: null, uploadId: null, accounts: null, upload: null, uploads: null, request: null };
let accFinancialCategories = [];
let accFinancialCalculations = [];
let accFinancialCalculationEditor = null;
let accNoteCategories = [];
let accCashflowCategories = [];
let accCashflowHistoricalOrphans = [];
let accCashflowTreasury = { cash: [], bank: [] };
let accCashflowSettings = { match_internal_transfers: false, receipt_presentation: 'net', source_template_id: null, role_categories: { customer_receipt: [], bank_charge: [], withholding_tax: [], other_deduction: [] } };
let accCashflowTemplateMapping = null;
let accFinancialLoaded = false;
let accAppliedFinancialAccountFilter = '';
let accAppliedFinancialCategoryFilter = '';
let accFinancialDraggingId = null;
let accNoteDraggingId = null;
let accNoteDraggingAnchorId = null;
let accNotesLoaded = false;
let accAppliedNotesAccountFilter = '';
let accAppliedNotesCategoryFilter = '';
let accCashflowLoaded = false;
let accIsAdmin = false;
let accBusyOperations = new Map();
let accBusySequence = 0;
let accTabLoading = { cashflow: false, financial: false, notes: false };
const accStatusTimers = new WeakMap();
const ACC_STATUS_DURATION_MS = 4200;

const ACC_WORKSTREAMS = [
    { value: 'cash', label: 'Cash vouchers' },
    { value: 'bank', label: 'Bank vouchers' },
    { value: 'local_vendor', label: 'Local vendor' },
    { value: 'import_vendor', label: 'Import vendor' },
    { value: 'export_customer', label: 'Export customer' },
    { value: 'payroll', label: 'Payroll' },
    { value: 'contractor', label: 'Contractor' },
];
function accElement(id) { return document.getElementById(id); }
function accEscape(value) {
    return String(value == null ? '' : value).replace(/[&<>'"]/g, function (character) {
        return ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;' })[character];
    });
}
function accNormaliseCode(value) { return String(value == null ? '' : value).trim(); }
function accSelectedUploadQuery() { return accSelectedUploadId ? '&trial_balance_upload_id=' + encodeURIComponent(accSelectedUploadId) : ''; }
function accSelectedUploadPayload() { return { trial_balance_upload_id: accSelectedUploadId ? Number(accSelectedUploadId) : null }; }
function accHideStatus(status) {
    if (!status) return;
    const timer = accStatusTimers.get(status);
    if (timer) window.clearTimeout(timer);
    accStatusTimers.delete(status);
    status.hidden = true;
}
function accGetPopupStatus(modal) {
    let status = modal.querySelector('[data-acc-popup-notice]');
    if (status) return status;
    status = document.createElement('div');
    status.className = 'acc2-status acc2-modal-notice';
    status.dataset.accPopupNotice = '';
    status.setAttribute('role', 'status');
    status.setAttribute('aria-live', 'polite');
    status.hidden = true;
    const header = modal.querySelector('.acc2-modal-header');
    if (header) header.insertAdjacentElement('afterend', status);
    else modal.insertBefore(status, modal.firstChild);
    return status;
}
function accShowStatus(message, type) {
    const mainStatus = accElement('accStatus');
    const openModal = document.querySelector('.acc2-modal-backdrop:not([hidden]) .acc2-modal');
    const status = openModal ? accGetPopupStatus(openModal) : mainStatus;
    if (status !== mainStatus) accHideStatus(mainStatus);
    document.querySelectorAll('[data-acc-popup-notice]').forEach(function (other) { if (other !== status) accHideStatus(other); });
    accHideStatus(status);
    status.textContent = message;
    status.className = status === mainStatus ? 'acc2-status acc2-main-toast' + (type ? ' ' + type : '') : 'acc2-status acc2-modal-notice' + (type ? ' ' + type : '');
    status.setAttribute('role', type === 'error' ? 'alert' : 'status');
    status.hidden = false;
    accStatusTimers.set(status, window.setTimeout(function () { status.hidden = true; accStatusTimers.delete(status); }, ACC_STATUS_DURATION_MS));
}
function accClearStatus() {
    accHideStatus(accElement('accStatus'));
    document.querySelectorAll('[data-acc-popup-notice]').forEach(accHideStatus);
}
function accRenderBusyState() {
    const busy = accElement('accPageBusy');
    if (!busy) return;
    const messages = Array.from(accBusyOperations.values());
    busy.hidden = !messages.length;
    busy.setAttribute('aria-busy', messages.length ? 'true' : 'false');
    if (messages.length) accElement('accPageBusyMessage').textContent = messages[0];
}
function accBeginBusy(message) {
    const token = ++accBusySequence;
    accBusyOperations.set(token, message);
    accRenderBusyState();
    return function () {
        accBusyOperations.delete(token);
        accRenderBusyState();
    };
}
async function accWithBusy(message, operation) {
    const endBusy = accBeginBusy(message);
    try { return await operation(); } finally { endBusy(); }
}
function accSetSelectBusy(select, isBusy) {
    if (!select || !select.isConnected) return;
    select.disabled = isBusy || !accIsAdmin;
    select.setAttribute('aria-busy', isBusy ? 'true' : 'false');
    const pickerTrigger = select.matches('.acc2-financial-category, .acc2-note-category') && select.closest('[data-fs-picker]') && select.closest('[data-fs-picker]').querySelector('[data-fs-picker-trigger]');
    if (pickerTrigger) pickerTrigger.disabled = isBusy || !accIsAdmin;
}
function accSetInlineBusy(select, result, message) {
    accSetSelectBusy(select, true);
    if (result) {
        result.classList.add('acc2-saving');
        result.innerHTML = '<span class="acc2-inline-saving"><span class="ui-spinner ui-spinner-sm" aria-hidden="true"></span><span>' + accEscape(message || 'Saving...') + '</span></span>';
    }
}
function accSetInlineResult(select, result, saved) {
    accSetSelectBusy(select, false);
    if (!result) return;
    result.classList.remove('acc2-saving');
    result.textContent = saved ? 'Saved' : 'Retry';
}
function accSetTableBusy(wrapId, isBusy) {
    const wrap = accElement(wrapId);
    if (!wrap) return;
    wrap.classList.toggle('acc2-processing', isBusy);
    wrap.setAttribute('aria-busy', isBusy ? 'true' : 'false');
}
async function accWithTableBusy(wrapId, message, operation) {
    accSetTableBusy(wrapId, true);
    try { return await accWithBusy(message, operation); } finally { accSetTableBusy(wrapId, false); }
}
function accSetButtonBusy(button, isBusy, message) {
    if (!button || !button.isConnected) return;
    if (isBusy) {
        button.dataset.accBusyLabel = button.textContent;
        button.dataset.accBusyDisabled = button.disabled ? 'true' : 'false';
        button.disabled = true;
        button.setAttribute('aria-busy', 'true');
        button.innerHTML = '<span class="ui-spinner ui-spinner-sm" aria-hidden="true"></span><span>' + accEscape(message || 'Saving...') + '</span>';
        return;
    }
    button.disabled = button.dataset.accBusyDisabled === 'true';
    button.setAttribute('aria-busy', 'false');
    button.textContent = button.dataset.accBusyLabel || button.textContent;
    delete button.dataset.accBusyLabel;
    delete button.dataset.accBusyDisabled;
}
async function accWithButtonBusy(button, message, operation) {
    accSetButtonBusy(button, true, message);
    try { return await accWithBusy(message, operation); } finally { accSetButtonBusy(button, false); }
}
function accSetState(stateId, wrapId, message, isError, isLoading) {
    const state = accElement(stateId);
    state.className = 'acc2-grid-state' + (isError ? ' error' : '') + (isLoading ? ' loading' : '');
    state.setAttribute('aria-busy', isLoading ? 'true' : 'false');
    if (isLoading) state.innerHTML = '<span class="ui-spinner ui-spinner-sm" aria-hidden="true"></span><span>' + accEscape(message) + '</span>';
    else state.textContent = message;
    state.hidden = false;
    accElement(wrapId).hidden = true;
}
function accSetStateReady(stateId) {
    const state = accElement(stateId);
    state.className = 'acc2-grid-state';
    state.setAttribute('aria-busy', 'false');
    state.hidden = true;
}
async function accApi(url, options) {
    const response = await fetch(url, options || {});
    const payload = await response.json().catch(function () { return {}; });
    if (!response.ok) throw new Error(payload.detail || payload.message || 'The request could not be completed.');
    return payload;
}
function accAssignment(account) { return account.assignment || {}; }
function accAccountIsWithin(account, parentCode) {
    const selectedCode = accNormaliseCode(parentCode);
    let ancestor = accNormaliseCode(account.account_code) === selectedCode
        ? selectedCode
        : accNormaliseCode(account.parent_account_code);
    const visited = new Set();
    while (ancestor && !visited.has(ancestor)) {
        if (ancestor === selectedCode) return true;
        visited.add(ancestor);
        const parent = accAccounts.find(function (candidate) {
            return accNormaliseCode(candidate.account_code) === ancestor;
        });
        ancestor = parent ? accNormaliseCode(parent.parent_account_code) : '';
    }
    return false;
}
function accLeavesBelow(parentCode) {
    return accAccounts.filter(function (account) {
        return account.is_leaf && accAccountIsWithin(account, parentCode);
    });
}
function accEffectiveWorkstream(account) {
    const direct = accAssignment(account).verification_workstream || '';
    if (account.is_leaf || direct) return direct;
    const values = Array.from(new Set(accLeavesBelow(account.account_code).map(function (leaf) {
        return accAssignment(leaf).verification_workstream || '';
    })));
    return values.length === 1 ? values[0] : (values.length > 1 ? '__mixed' : '');
}
function accEffectiveFinancialCategory(account) {
    const direct = accAssignment(account).financial_statement_category_id;
    if (account.is_leaf || direct) return direct == null ? '' : String(direct);
    const values = Array.from(new Set(accLeavesBelow(account.account_code).map(function (leaf) {
        const value = accAssignment(leaf).financial_statement_category_id;
        return value == null ? '' : String(value);
    })));
    return values.length === 1 ? values[0] : (values.length > 1 ? '__mixed' : '');
}
function accEffectiveNoteCategory(account) {
    const direct = accAssignment(account).note_category_id;
    if (account.is_leaf || direct != null) return direct == null ? '' : String(direct);
    const values = Array.from(new Set(accLeavesBelow(account.account_code).map(function (leaf) {
        const value = accAssignment(leaf).note_category_id;
        return value == null ? '' : String(value);
    })));
    return values.length === 1 ? values[0] : (values.length > 1 ? '__mixed' : '');
}
function accFilterAccountRows(accountQuery, assignmentQuery, getAssignmentText) {
    const accountTerm = String(accountQuery || '').trim().toLowerCase();
    const assignmentTerm = String(assignmentQuery || '').trim().toLowerCase();
    const matched = accAccounts.filter(function (account) {
        const accountText = (String(account.account_code || '') + ' ' + String(account.account_name || '')).toLowerCase();
        const assignmentText = String(getAssignmentText(account) || '').toLowerCase();
        return (!accountTerm || accountText.indexOf(accountTerm) !== -1) && (!assignmentTerm || assignmentText.indexOf(assignmentTerm) !== -1);
    });
    const visible = new Set(matched);
    if (accountTerm || assignmentTerm) {
        matched.forEach(function (match) {
            accAccounts.forEach(function (candidate) {
                if (!candidate.is_leaf && accAccountIsWithin(match, candidate.account_code)) visible.add(candidate);
            });
        });
    }
    return accAccounts.filter(function (account) { return visible.has(account); });
}
function accOptionMarkup(selected) {
    let html = '<option value="">Unassigned</option>';
    if (selected === '__mixed') html = '<option value="__mixed" selected>Mixed assignments - choose one</option>' + html;
    html += ACC_WORKSTREAMS.map(function (item) {
        return '<option value="' + item.value + '"' + (item.value === selected ? ' selected' : '') + '>' + item.label + '</option>';
    }).join('');
    return html;
}
function accCategoryOptionMarkupFor(categories, selected) {
    let html = '<option value="">Unassigned</option>';
    if (selected === '__mixed') html = '<option value="__mixed" selected>Mixed assignments - choose one</option>' + html;
    html += categories.filter(function (category) { return category.row_type !== 'heading'; }).map(function (category) {
        const value = String(category.id);
        const inactive = !category.is_active;
        const parent = categories.find(function (candidate) { return Number(candidate.id) === Number(category.parent_category_id); });
        const section = category.statement_section === 'balance_sheet' ? 'Balance Sheet' : (category.statement_section === 'income_statement' ? 'Income Statement' : 'Unplaced');
        const label = (inactive ? 'Inactive: ' : '') + (parent ? parent.label + ' › ' : '') + category.label + ' · ' + section;
        return '<option value="' + accEscape(value) + '"' + (value === String(selected) ? ' selected' : '') + (inactive && value !== String(selected) ? ' disabled' : '') + '>' + accEscape(label) + '</option>';
    }).join('');
    return html;
}
function accCategoryOptionMarkup(selected) { return accCategoryOptionMarkupFor(accFinancialCategories, selected); }
function accCashflowCategoryOptionMarkup(selected) { return accCategoryOptionMarkupFor(accCashflowCategories, selected); }
function accFinancialPickerLabel(categoryId) {
    if (categoryId === '__mixed') return 'Mixed assignments — choose one';
    if (!categoryId) return 'Unassigned';
    const category = accFinancialCategories.find(function (item) { return Number(item.id) === Number(categoryId) && item.row_type !== 'heading'; });
    if (!category) return 'Unassigned';
    const parent = accFinancialCategories.find(function (item) { return Number(item.id) === Number(category.parent_category_id) && item.row_type === 'heading'; });
    const section = category.statement_section === 'balance_sheet' ? 'BS' : (category.statement_section === 'income_statement' ? 'IS' : 'Legacy');
    return section + ' · ' + (!category.is_active ? 'Inactive · ' : '') + (parent ? parent.label + ' › ' : '') + category.label;
}
function accFinancialPickerOptionMarkup(category, parent, sectionLabel, selectedId) {
    const inactive = !category.is_active;
    const search = [category.label, parent && parent.label, sectionLabel].filter(Boolean).join(' ').toLowerCase();
    return '<button type="button" class="acc2-fs-picker-option' + (inactive ? ' inactive' : '') + '" role="option" data-fs-picker-option data-fs-picker-value="' + category.id + '" data-fs-picker-search="' + accEscape(search) + '" aria-selected="' + (String(selectedId) === String(category.id) ? 'true' : 'false') + '"' + (inactive ? ' disabled aria-disabled="true"' : '') + '><span>' + (inactive ? '<span class="acc2-fs-picker-inactive">Inactive</span> ' : '') + accEscape(category.label) + '</span><span class="acc2-fs-picker-check" aria-hidden="true">✓</span></button>';
}
function accFinancialPickerSectionMarkup(section, label, categories, selectedId) {
    const rows = categories.filter(function (item) { return item.statement_section === section && item.row_type !== 'heading'; });
    const headings = rows.filter(function (item) { return Number(item.parent_category_id) > 0; }).map(function (item) {
        return categories.find(function (candidate) { return Number(candidate.id) === Number(item.parent_category_id) && candidate.row_type === 'heading' && candidate.statement_section === section; });
    }).filter(Boolean).filter(function (item, index, items) { return items.findIndex(function (candidate) { return Number(candidate.id) === Number(item.id); }) === index; }).sort(function (a, b) { return Number(a.sort_order || 0) - Number(b.sort_order || 0); });
    let content = '';
    headings.forEach(function (heading) {
        const children = rows.filter(function (item) { return Number(item.parent_category_id) === Number(heading.id); }).sort(function (a, b) { return Number(a.sort_order || 0) - Number(b.sort_order || 0); });
        if (!children.length) return;
        content += '<div class="acc2-fs-picker-heading-group" data-fs-picker-heading-group><div class="acc2-fs-picker-heading" role="presentation">' + accEscape(heading.label) + '</div>' + children.map(function (item) { return accFinancialPickerOptionMarkup(item, heading, label, selectedId); }).join('') + '</div>';
    });
    const standalone = rows.filter(function (item) { return !headings.some(function (heading) { return Number(heading.id) === Number(item.parent_category_id); }); }).sort(function (a, b) { return Number(a.sort_order || 0) - Number(b.sort_order || 0); });
    if (standalone.length) content += '<div class="acc2-fs-picker-heading-group" data-fs-picker-heading-group><div class="acc2-fs-picker-heading" role="presentation">Other ' + accEscape(label) + ' categories</div>' + standalone.map(function (item) { return accFinancialPickerOptionMarkup(item, null, label, selectedId); }).join('') + '</div>';
    if (!content) content = '<div class="acc2-fs-picker-empty-group" role="presentation">No ' + accEscape(label) + ' categories yet.</div>';
    return '<div class="acc2-fs-picker-section" data-fs-picker-section data-fs-picker-section-key="' + section + '"><div class="acc2-fs-picker-section-title" role="presentation">' + accEscape(label) + '</div>' + content + '</div>';
}
function accFinancialPickerMenuMarkup(selectedId) {
    const categories = accFinancialCategories;
    let html = '<div class="acc2-fs-picker-option-wrap" data-fs-picker-option-wrap role="listbox" aria-label="Financial Statement categories">';
    html += '<button type="button" class="acc2-fs-picker-option" role="option" data-fs-picker-option data-fs-picker-value="" data-fs-picker-search="unassigned" aria-selected="' + (!selectedId ? 'true' : 'false') + '"><span>Unassigned</span><span class="acc2-fs-picker-check" aria-hidden="true">✓</span></button>';
    if (selectedId === '__mixed') html += '<div class="acc2-fs-picker-mixed" role="presentation">Mixed assignments — choose one</div>';
    html += accFinancialPickerSectionMarkup('balance_sheet', 'Balance Sheet', categories, selectedId);
    html += accFinancialPickerSectionMarkup('income_statement', 'Income Statement', categories, selectedId);
    const unplaced = categories.filter(function (item) { return item.row_type !== 'heading' && !item.statement_section; }).sort(function (a, b) { return Number(a.sort_order || 0) - Number(b.sort_order || 0); });
    if (unplaced.length) {
        html += '<div class="acc2-fs-picker-section" data-fs-picker-section data-fs-picker-section-key="unplaced"><div class="acc2-fs-picker-section-title muted" role="presentation">Unplaced / legacy categories</div>';
        html += '<div class="acc2-fs-picker-heading-group" data-fs-picker-heading-group>' + unplaced.map(function (item) { return accFinancialPickerOptionMarkup(item, null, 'Unplaced categories', selectedId); }).join('') + '</div></div>';
    }
    return html + '<div class="acc2-fs-picker-no-results" data-fs-picker-no-results hidden>No matching categories.</div></div>';
}
function accFinancialPickerMarkup(account, selectedId) {
    const codeKey = String(account.account_code || '').replace(/[^a-zA-Z0-9_-]/g, '_');
    const pickerId = 'accFsPicker_' + codeKey;
    return '<div class="acc2-fs-picker" data-fs-picker>' +
        '<button type="button" class="acc2-fs-picker-trigger" data-fs-picker-trigger role="combobox" aria-haspopup="listbox" aria-expanded="false" aria-controls="' + pickerId + '_menu" aria-label="Financial Statement category for account ' + accEscape(account.account_code) + '"' + (!accIsAdmin ? ' disabled' : '') + '><span data-fs-picker-value-label>' + accEscape(accFinancialPickerLabel(selectedId)) + '</span><span class="acc2-fs-picker-chevron" aria-hidden="true">▾</span></button>' +
        '<div class="acc2-fs-picker-menu" id="' + pickerId + '_menu" data-fs-picker-menu hidden><label class="acc2-fs-picker-search-label" for="' + pickerId + '_search">Find a category</label><input type="search" class="acc2-fs-picker-search" id="' + pickerId + '_search" data-fs-picker-search-input placeholder="Search categories or headings" autocomplete="off">' + accFinancialPickerMenuMarkup(selectedId) + '</div>' +
        '<select class="acc2-financial-category" data-acc-code="' + accEscape(account.account_code) + '" data-acc-parent="' + (!account.is_leaf ? 'true' : 'false') + '" aria-hidden="true" tabindex="-1" hidden' + (!accIsAdmin ? ' disabled' : '') + '>' + accCategoryOptionMarkup(selectedId) + '</select></div>';
}
const ACC_CASHFLOW_ROLE_DEFINITIONS = [
    { key: 'customer_receipt', controlId: 'accCashflowCustomerReceiptCategories', menuId: 'accCashflowCustomerReceiptMenu', optionsId: 'accCashflowCustomerReceiptOptions', searchId: 'accCashflowCustomerReceiptSearch', label: 'Customer receipt categories' },
    { key: 'bank_charge', controlId: 'accCashflowBankChargeCategories', menuId: 'accCashflowBankChargeMenu', optionsId: 'accCashflowBankChargeOptions', searchId: 'accCashflowBankChargeSearch', label: 'Bank charge categories' },
    { key: 'withholding_tax', controlId: 'accCashflowWithholdingCategories', menuId: 'accCashflowWithholdingMenu', optionsId: 'accCashflowWithholdingOptions', searchId: 'accCashflowWithholdingSearch', label: 'Withholding tax categories' },
    { key: 'other_deduction', controlId: 'accCashflowOtherDeductionCategories', menuId: 'accCashflowOtherDeductionMenu', optionsId: 'accCashflowOtherDeductionOptions', searchId: 'accCashflowOtherDeductionSearch', label: 'Other deduction categories' },
];
function accEnsureCashflowRoleControls() {
    if (accElement(ACC_CASHFLOW_ROLE_DEFINITIONS[0].controlId)) return;
    const panel = document.querySelector('[data-acc-cashflow-config-panel="customer"]');
    if (!panel) return;
    const grid = document.createElement('div');
    grid.className = 'acc2-customer-role-grid';
    grid.innerHTML = ACC_CASHFLOW_ROLE_DEFINITIONS.map(function (definition) {
        return '<div class="acc2-cashflow-role"><label for="' + definition.controlId + '">' + accEscape(definition.label) + '</label><button type="button" id="' + definition.controlId + '" class="acc2-cashflow-role-toggle" data-acc-cashflow-role-toggle="' + definition.key + '" aria-expanded="false"><span data-acc-cashflow-role-summary="' + definition.key + '">Select categories</span><span aria-hidden="true">▾</span></button><div id="' + definition.menuId + '" class="acc2-cashflow-role-menu" data-acc-cashflow-role-menu="' + definition.key + '" hidden><div class="acc2-cashflow-role-menu-tools"><input id="' + definition.searchId + '" type="search" placeholder="Search categories" data-acc-cashflow-role-search="' + definition.key + '"><button type="button" class="acc2-cashflow-role-clear" data-acc-cashflow-role-clear="' + definition.key + '">Clear</button></div><div id="' + definition.optionsId + '" class="acc2-cashflow-role-options" data-acc-cashflow-role-options="' + definition.key + '"></div></div></div>';
    }).join('');
    panel.appendChild(grid);
    const save = document.createElement('button');
    save.type = 'button';
    save.id = 'accCashflowCustomerSettingsSave';
    save.className = 'acc2-action-btn';
    save.textContent = 'Save customer payment categories';
    panel.appendChild(save);
}
function accCloseCashflowRoleMenus(exceptRole) {
    document.querySelectorAll('[data-acc-cashflow-role-menu]').forEach(function (menu) {
        const role = menu.dataset.accCashflowRoleMenu;
        const open = role === exceptRole;
        menu.hidden = !open;
        const toggle = document.querySelector('[data-acc-cashflow-role-toggle="' + CSS.escape(role) + '"]');
        if (toggle) toggle.setAttribute('aria-expanded', open ? 'true' : 'false');
    });
}
function accUpdateCashflowRoleSummary(role) {
    const definition = ACC_CASHFLOW_ROLE_DEFINITIONS.find(function (item) { return item.key === role; });
    if (!definition) return;
    const options = accElement(definition.optionsId);
    const summary = document.querySelector('[data-acc-cashflow-role-summary="' + CSS.escape(role) + '"]');
    if (!options || !summary) return;
    const selected = Array.from(options.querySelectorAll('input[type="checkbox"]:checked'));
    if (!selected.length) summary.textContent = 'Select categories';
    else if (selected.length === 1) summary.textContent = selected[0].dataset.label || selected[0].value;
    else summary.textContent = selected.length + ' categories selected';
}
function accHandleCashflowRoleClick(event) {
    const toggle = event.target.closest('[data-acc-cashflow-role-toggle]');
    if (toggle) {
        const role = toggle.dataset.accCashflowRoleToggle;
        const menu = accElement(ACC_CASHFLOW_ROLE_DEFINITIONS.find(function (item) { return item.key === role; }).menuId);
        const shouldOpen = menu && menu.hidden;
        accCloseCashflowRoleMenus(shouldOpen ? role : null);
        return;
    }
    const clear = event.target.closest('[data-acc-cashflow-role-clear]');
    if (clear) {
        const definition = ACC_CASHFLOW_ROLE_DEFINITIONS.find(function (item) { return item.key === clear.dataset.accCashflowRoleClear; });
        const options = definition && accElement(definition.optionsId);
        if (options) options.querySelectorAll('input[type="checkbox"]').forEach(function (input) { input.checked = false; });
        if (definition) accUpdateCashflowRoleSummary(definition.key);
        return;
    }
    if (!event.target.closest('.acc2-cashflow-role')) accCloseCashflowRoleMenus(null);
}
function accHandleCashflowRoleSearch(event) {
    const search = event.target.closest('[data-acc-cashflow-role-search]');
    if (!search) return;
    const definition = ACC_CASHFLOW_ROLE_DEFINITIONS.find(function (item) { return item.key === search.dataset.accCashflowRoleSearch; });
    const options = definition && accElement(definition.optionsId);
    const term = String(search.value || '').trim().toLowerCase();
    if (!options) return;
    options.querySelectorAll('[data-acc-cashflow-role-option]').forEach(function (option) {
        option.hidden = term && !String(option.dataset.label || '').toLowerCase().includes(term);
    });
}
function accHandleCashflowRoleChange(event) {
    const input = event.target.closest('[data-acc-cashflow-role-input]');
    if (input) accUpdateCashflowRoleSummary(input.dataset.accCashflowRoleInput);
}
function accCashflowRoleCategoryIds(role) {
    const settings = accCashflowSettings && accCashflowSettings.role_categories;
    return Array.isArray(settings && settings[role]) ? settings[role].map(function (value) { return Number(value); }).filter(function (value) { return Number.isInteger(value) && value > 0; }) : [];
}
function accRenderCashflowRoleCategories() {
    accEnsureCashflowRoleControls();
    ACC_CASHFLOW_ROLE_DEFINITIONS.forEach(function (definition) {
        const options = accElement(definition.optionsId);
        if (!options) return;
        const selected = new Set(accCashflowRoleCategoryIds(definition.key));
        const categories = accCashflowCategories.filter(function (category) { return category.is_active !== false && Number(category.is_active) !== 0; });
        options.innerHTML = categories.length ? categories.map(function (category) {
            const id = Number(category.id);
            const label = category.label || category.category_key || ('Category ' + id);
            return '<label class="acc2-cashflow-role-option" data-acc-cashflow-role-option data-label="' + accEscape(label) + '"><input type="checkbox" value="' + accEscape(id) + '" data-acc-cashflow-role-input="' + definition.key + '" data-label="' + accEscape(label) + '"' + (selected.has(id) ? ' checked' : '') + (!accIsAdmin ? ' disabled' : '') + '><span>' + accEscape(label) + '</span></label>';
        }).join('') : '<span class="acc2-cashflow-role-empty">No active categories</span>';
        const toggle = accElement(definition.controlId);
        if (toggle) toggle.disabled = !accIsAdmin || !categories.length;
        accUpdateCashflowRoleSummary(definition.key);
    });
}
function accCollectCashflowRoleCategories() {
    const roles = {};
    ACC_CASHFLOW_ROLE_DEFINITIONS.forEach(function (definition) {
        const options = accElement(definition.optionsId);
        roles[definition.key] = options ? Array.from(options.querySelectorAll('input[type="checkbox"]:checked')).map(function (input) { return Number(input.value); }).filter(function (value) { return Number.isInteger(value) && value > 0; }) : [];
    });
    return roles;
}
const ACC_CASHFLOW_COMBO_ROLE_DEFINITIONS = [
    { key: 'customer_receipt', inputId: 'accCashflowCustomerReceiptCategories', listId: 'accCashflowCustomerReceiptCategoryOptions', label: 'Customer receipt categories' },
    { key: 'bank_charge', inputId: 'accCashflowBankChargeCategories', listId: 'accCashflowBankChargeCategoryOptions', label: 'Bank charge categories' },
    { key: 'withholding_tax', inputId: 'accCashflowWithholdingCategories', listId: 'accCashflowWithholdingCategoryOptions', label: 'Withholding tax categories' },
    { key: 'other_deduction', inputId: 'accCashflowOtherDeductionCategories', listId: 'accCashflowOtherDeductionCategoryOptions', label: 'Other deduction categories' },
];
function accEnsureCashflowComboboxControls() {
    if (accElement(ACC_CASHFLOW_COMBO_ROLE_DEFINITIONS[0].inputId)) return;
    const panel = document.querySelector('[data-acc-cashflow-config-panel="customer"]');
    if (!panel) return;
    const grid = document.createElement('div');
    grid.className = 'acc2-customer-role-grid';
    grid.innerHTML = ACC_CASHFLOW_COMBO_ROLE_DEFINITIONS.map(function (definition) {
        return '<div class="acc2-cashflow-role"><label for="' + definition.inputId + '">' + accEscape(definition.label) + '</label><input id="' + definition.inputId + '" class="acc2-cashflow-role-input" list="' + definition.listId + '" type="search" placeholder="Assign category" autocomplete="off" data-acc-cashflow-role-input="' + definition.key + '"><datalist id="' + definition.listId + '"></datalist></div>';
    }).join('');
    panel.appendChild(grid);
    if (!accElement('accCashflowCustomerSettingsSave')) {
        const save = document.createElement('button');
        save.type = 'button';
        save.id = 'accCashflowCustomerSettingsSave';
        save.className = 'acc2-action-btn';
        save.textContent = 'Save customer payment categories';
        panel.appendChild(save);
    }
}
function accHandleCashflowComboboxInput(event) {
    const input = event.target.closest('[data-acc-cashflow-role-input]');
    if (!input) return;
    const categories = accCashflowCategories.filter(function (category) { return category.is_active !== false && Number(category.is_active) !== 0; });
    const byLabel = new Map();
    categories.forEach(function (category) {
        const label = category.label || category.category_key || '';
        byLabel.set(String(label).trim().toLowerCase(), Number(category.id));
    });
    const tokens = String(input.value || '').split(',').map(function (value) { return value.trim(); }).filter(Boolean);
    const selected = tokens.map(function (value) { return byLabel.get(value.toLowerCase()); });
    if (!tokens.length || selected.every(function (value) { return Number.isInteger(value) && value > 0; })) {
        input.dataset.selectedIds = selected.filter(function (value) { return Number.isInteger(value) && value > 0; }).join(',');
    }
}
function accRenderCashflowComboboxCategories() {
    accEnsureCashflowComboboxControls();
    ACC_CASHFLOW_COMBO_ROLE_DEFINITIONS.forEach(function (definition) {
        const input = accElement(definition.inputId);
        const datalist = accElement(definition.listId);
        if (!input || !datalist) return;
        const selected = new Set(accCashflowRoleCategoryIds(definition.key));
        const categories = accCashflowCategories.filter(function (category) { return category.is_active !== false && Number(category.is_active) !== 0; });
        datalist.innerHTML = categories.map(function (category) {
            const label = category.label || category.category_key || ('Category ' + category.id);
            return '<option value="' + accEscape(label) + '">';
        }).join('');
        input.value = categories.filter(function (category) { return selected.has(Number(category.id)); }).map(function (category) { return category.label || category.category_key || ''; }).join(', ');
        input.dataset.selectedIds = Array.from(selected).join(',');
        input.disabled = !accIsAdmin || !categories.length;
    });
}
function accCollectCashflowComboboxCategories() {
    const roles = {};
    ACC_CASHFLOW_COMBO_ROLE_DEFINITIONS.forEach(function (definition) {
        const input = accElement(definition.inputId);
        roles[definition.key] = input ? String(input.dataset.selectedIds || '').split(',').map(function (value) { return Number(value); }).filter(function (value) { return Number.isInteger(value) && value > 0; }) : [];
    });
    return roles;
}
function accSetGridState(message, isError, isLoading) { accSetState('accGridState', 'accGridWrap', message, isError, isLoading); }
function accSetFinancialGridState(message, isError, isLoading) { accSetState('accFinancialGridState', 'accFinancialGridWrap', message, isError, isLoading); }
function accSetNotesGridState(message, isError, isLoading) { accSetState('accNotesGridState', 'accNotesGridWrap', message, isError, isLoading); }
function accRenderRows() {
    const filter = String(accElement('accAccountFilter').value || '').trim().toLowerCase();
    const visible = accAccounts.filter(function (account) {
        return !filter || (String(account.account_code || '') + ' ' + String(account.account_name || '')).toLowerCase().indexOf(filter) !== -1;
    });
    accElement('accAccountCount').textContent = visible.length + ' of ' + accAccounts.length + ' accounts';
    const rows = accElement('accAccountRows');
    if (!visible.length) {
        rows.innerHTML = '<tr><td colspan="5" class="acc2-empty">No accounts match this search.</td></tr>';
    } else {
        rows.innerHTML = visible.map(function (account) {
            const isParent = !account.is_leaf;
            const selected = accEffectiveWorkstream(account);
            const indent = Math.max(0, Number(account.level || 1) - 1) * 16;
            const type = isParent ? 'Parent' : 'Leaf';
            const result = isParent ? '<span class="acc2-parent-help">Applies to children</span>' : '<span class="acc2-saved" data-acc-result="' + accEscape(account.account_code) + '">Saved</span>';
            return '<tr class="' + (isParent ? 'acc2-parent' : '') + '">' +
                '<td class="acc2-code" style="padding-left:' + (indent + 10) + 'px">' + accEscape(account.account_code) + '</td>' +
                '<td>' + accEscape(account.account_name || 'Unknown account') + '</td>' +
                '<td><span class="acc2-type ' + (isParent ? 'parent' : 'leaf') + '">' + type + '</span></td>' +
                '<td><select class="acc2-workstream" data-acc-code="' + accEscape(account.account_code) + '" data-acc-parent="' + (isParent ? 'true' : 'false') + '"' + (!accIsAdmin ? ' disabled' : '') + '>' + accOptionMarkup(selected) + '</select></td>' +
                '<td>' + result + '</td>' +
                '</tr>';
        }).join('');
    }
    accSetStateReady('accGridState');
    accElement('accGridWrap').hidden = false;
}
function accSetCashflowGridState(message, isError, isLoading) { accSetState('accCashflowGridState', 'accCashflowGridWrap', message, isError, isLoading); }
function accEffectiveCashflowCategory(account) {
    const direct = account.category_id;
    if (account.is_leaf || direct != null) return direct == null ? '' : String(direct);
    const values = Array.from(new Set(accCashflowLeavesBelow(account.account_code).map(function (leaf) {
        return leaf.category_id == null ? '' : String(leaf.category_id);
    })));
    return values.length === 1 ? values[0] : (values.length > 1 ? '__mixed' : '');
}
function accCashflowLeavesBelow(parentCode) {
    const currentLeaves = accLeavesBelow(parentCode);
    const normalisedParent = accNormaliseCode(parentCode);
    const historicalLeaves = accCashflowHistoricalOrphans.filter(function (orphan) {
        const survivingParent = accNormaliseCode(orphan.parent_account_code);
        return survivingParent && (survivingParent === normalisedParent || survivingParent.indexOf(normalisedParent + '-') === 0);
    });
    return currentLeaves.concat(historicalLeaves);
}
function accCashflowRowIndent(account, isHistorical) {
    if (!isHistorical) return Math.max(0, Number(account.level || 1) - 1) * 16;
    const parent = accAccounts.find(function (candidate) {
        return accNormaliseCode(candidate.account_code) === accNormaliseCode(account.parent_account_code);
    });
    const level = parent ? Number(parent.level || 1) + 1 : Number(account.level || 1);
    return Math.max(0, level - 1) * 16;
}
function accCashflowRowMarkup(account, isHistorical) {
    const isParent = !isHistorical && !account.is_leaf;
    const selected = isParent ? accEffectiveCashflowCategory(account) : (account.category_id == null ? '' : String(account.category_id));
    const rowClasses = [];
    if (isParent) rowClasses.push('acc2-parent');
    if (isHistorical) rowClasses.push('acc2-historical-orphan');
    const typeClass = isHistorical ? 'historical' : (isParent ? 'parent' : 'leaf');
    const typeLabel = isHistorical ? 'Historical / orphan' : (isParent ? 'Parent' : 'Leaf');
    const result = isParent ? '<span class="acc2-parent-help">Applies to children</span>' : '<span class="acc2-saved" data-acc-cashflow-result="' + accEscape(account.account_code) + '">Saved</span>';
    const historicalAttribute = isHistorical ? ' data-acc-historical-orphan="true"' : '';
    return '<tr class="' + rowClasses.join(' ') + '"' + historicalAttribute + '>' +
        '<td class="acc2-code" style="padding-left:' + (accCashflowRowIndent(account, isHistorical) + 10) + 'px">' + accEscape(account.account_code) + '</td>' +
        '<td>' + accEscape(account.account_name || (isHistorical ? 'Historical account' : 'Unknown account')) + '</td>' +
        '<td><span class="acc2-type ' + typeClass + '">' + typeLabel + '</span></td>' +
        '<td><select class="acc2-cashflow-category" data-acc-code="' + accEscape(account.account_code) + '" data-acc-parent="' + (isParent ? 'true' : 'false') + '"' + (isHistorical ? ' data-acc-historical-orphan="true"' : '') + (!accIsAdmin ? ' disabled' : '') + '>' + accCashflowCategoryOptionMarkup(selected) + '</select></td>' +
        '<td>' + result + '</td>' +
        '</tr>';
}
function accCashflowSearchMatches(account, search, isHistorical) {
    const values = [String(account.account_code || ''), String(account.account_name || '')];
    const categoryIds = [];
    const addCategory = function (categoryId) {
        if (categoryId == null) values.push('unassigned');
        else categoryIds.push(String(categoryId));
    };
    if (isHistorical || account.is_leaf || account.category_id != null) {
        addCategory(account.category_id);
    } else {
        accCashflowLeavesBelow(account.account_code).forEach(function (leaf) { addCategory(leaf.category_id); });
    }
    accCashflowCategories.forEach(function (category) {
        if (categoryIds.indexOf(String(category.id)) !== -1) values.push(category.category_key, category.label);
    });
    return values.join(' ').toLowerCase().indexOf(search) !== -1;
}
function accRenderCashflowRows() {
    const filter = String(accElement('accCashflowFilter').value || '').trim().toLowerCase();
    const visible = accAccounts.filter(function (account) {
        return !filter || accCashflowSearchMatches(account, filter, false);
    });
    const visibleOrphans = accCashflowHistoricalOrphans.filter(function (orphan) {
        return !filter || accCashflowSearchMatches(orphan, filter, true);
    });
    accElement('accCashflowCount').textContent = (visible.length + visibleOrphans.length) + ' of ' + (accAccounts.length + accCashflowHistoricalOrphans.length) + ' accounts';
    accElement('accCashflowOrphanNotice').hidden = !accCashflowHistoricalOrphans.length;
    const rows = accElement('accCashflowRows');
    const orphanByInsertIndex = {};
    visibleOrphans.forEach(function (orphan) {
        const parentCode = accNormaliseCode(orphan.parent_account_code);
        let insertIndex = -1;
        if (parentCode) {
            visible.forEach(function (account, index) {
                const code = accNormaliseCode(account.account_code);
                if (code === parentCode || code.indexOf(parentCode + '-') === 0) insertIndex = index + 1;
            });
        }
        const key = String(insertIndex < 0 ? visible.length : insertIndex);
        if (!orphanByInsertIndex[key]) orphanByInsertIndex[key] = [];
        orphanByInsertIndex[key].push(orphan);
    });
    if (!visible.length && !visibleOrphans.length) {
        rows.innerHTML = '<tr><td colspan="5" class="acc2-empty">No accounts match this search.</td></tr>';
    } else {
        const rendered = [];
        if (!visible.length) {
            (orphanByInsertIndex['0'] || []).forEach(function (orphan) { rendered.push(accCashflowRowMarkup(orphan, true)); });
        } else {
            visible.forEach(function (account, index) {
                rendered.push(accCashflowRowMarkup(account, false));
                (orphanByInsertIndex[String(index + 1)] || []).forEach(function (orphan) { rendered.push(accCashflowRowMarkup(orphan, true)); });
            });
        }
        rows.innerHTML = rendered.join('');
    }
    accSetStateReady('accCashflowGridState');
    accElement('accCashflowGridWrap').hidden = false;
}
function accRenderFinancialRows() {
    const visible = accFilterAccountRows(accAppliedFinancialAccountFilter, accAppliedFinancialCategoryFilter, function (account) {
        return accFinancialPickerLabel(accEffectiveFinancialCategory(account));
    });
    accElement('accFinancialCount').textContent = visible.length + ' of ' + accAccounts.length + ' accounts';
    const rows = accElement('accFinancialRows');
    if (!visible.length) {
        rows.innerHTML = '<tr><td colspan="5" class="acc2-empty">No accounts match these filters.</td></tr>';
    } else {
        rows.innerHTML = visible.map(function (account) {
            const isParent = !account.is_leaf;
            const selected = accEffectiveFinancialCategory(account);
            const indent = Math.max(0, Number(account.level || 1) - 1) * 16;
            const result = isParent ? '<span class="acc2-parent-help">Applies to children</span>' : '<span class="acc2-saved" data-acc-fs-result="' + accEscape(account.account_code) + '">Saved</span>';
            return '<tr class="' + (isParent ? 'acc2-parent' : '') + '">' +
                '<td class="acc2-code" style="padding-left:' + (indent + 10) + 'px">' + accEscape(account.account_code) + '</td>' +
                '<td>' + accEscape(account.account_name || 'Unknown account') + '</td>' +
                '<td><span class="acc2-type ' + (isParent ? 'parent' : 'leaf') + '">' + (isParent ? 'Parent' : 'Leaf') + '</span></td>' +
                '<td>' + accFinancialPickerMarkup(account, selected) + '</td>' +
                '<td>' + result + '</td>' +
                '</tr>';
        }).join('');
    }
    accSetStateReady('accFinancialGridState');
    accElement('accFinancialGridWrap').hidden = false;
}
function accGetEffectiveNoteOptions(statementCategoryId) {
    const category = accFinancialCategories.find(function (item) { return Number(item.id) === Number(statementCategoryId) && item.row_type !== 'heading'; });
    if (!category) return [];
    const root = accNoteCategories.find(function (item) { return Number(item.source_statement_category_id) === Number(category.id); });
    if (!root || root.is_active === false || Number(root.is_active) === 0) return [];
    const options = [];
    function visit(note, path, isLinkedRoot) {
        const children = accNoteCategories.filter(function (item) {
            return Number(item.parent_category_id) === Number(note.id) && item.is_active !== false && Number(item.is_active) !== 0;
        }).sort(function (a, b) { return Number(a.sort_order || 0) - Number(b.sort_order || 0); });
        const nextPath = path.concat([isLinkedRoot ? category.label : note.label]);
        if (!children.length) {
            options.push({ category: note, path: nextPath });
            return;
        }
        children.forEach(function (child) { visit(child, nextPath, false); });
    }
    visit(root, [], true);
    return options;
}
function accNotePickerLabel(statementCategoryId, selectedId, options) {
    if (statementCategoryId === '__mixed') return 'Accounts span multiple statement categories';
    if (!statementCategoryId) return 'Assign a Financial Statement category first';
    if (!options.length) return 'No notes defined under this category';
    if (selectedId === '__mixed') return 'Mixed assignments — choose one';
    if (!selectedId) return 'Select a note';
    const selected = options.find(function (item) { return String(item.category.id) === String(selectedId); });
    return selected ? selected.path.join(' › ') : 'Existing note assignment';
}
function accNoteAssignmentSearchText(account) {
    const statementCategoryId = accEffectiveFinancialCategory(account);
    const noteCategoryId = accEffectiveNoteCategory(account);
    if (noteCategoryId === '__mixed') return 'Mixed assignments';
    if (!noteCategoryId) return 'Unassigned';
    const options = statementCategoryId && statementCategoryId !== '__mixed' ? accGetEffectiveNoteOptions(statementCategoryId) : [];
    const selected = options.find(function (item) { return String(item.category.id) === String(noteCategoryId); });
    if (selected) return selected.path.join(' ');
    const existing = accNoteCategories.find(function (item) { return Number(item.id) === Number(noteCategoryId); });
    return existing ? existing.label : 'Existing note assignment';
}
function accNotePickerMarkup(account, selectedId, statementCategoryId, options) {
    const codeKey = String(account.account_code || '').replace(/[^a-zA-Z0-9_-]/g, '_');
    const pickerId = 'accNotePicker_' + codeKey;
    const source = statementCategoryId && statementCategoryId !== '__mixed'
        ? accFinancialCategories.find(function (item) { return Number(item.id) === Number(statementCategoryId); })
        : null;
    const disabled = !accIsAdmin || !source || !options.length;
    const label = accNotePickerLabel(statementCategoryId, selectedId, options);
    let optionMarkup = '';
    let selectMarkup = '<option value="">Unassigned</option>';
    if (selectedId === '__mixed') selectMarkup = '<option value="__mixed" selected>Mixed assignments - choose one</option>' + selectMarkup;
    if (options.length) {
        optionMarkup += '<div class="acc2-fs-picker-section" data-fs-picker-section><div class="acc2-fs-picker-section-title" role="presentation">' + accEscape(source.label) + ' notes</div>';
        optionMarkup += '<div class="acc2-fs-picker-heading-group" data-fs-picker-heading-group><button type="button" class="acc2-fs-picker-option" role="option" data-fs-picker-option data-fs-picker-value="" data-fs-picker-search="unassigned" aria-selected="' + (!selectedId ? 'true' : 'false') + '"><span>Unassigned</span><span class="acc2-fs-picker-check" aria-hidden="true">✓</span></button>';
        options.forEach(function (item) {
            const id = String(item.category.id);
            const path = item.path.join(' › ');
            optionMarkup += '<button type="button" class="acc2-fs-picker-option" role="option" data-fs-picker-option data-fs-picker-value="' + id + '" data-fs-picker-search="' + accEscape(path.toLowerCase()) + '" aria-selected="' + (String(selectedId) === id ? 'true' : 'false') + '"><span>' + accEscape(path) + '</span><span class="acc2-fs-picker-check" aria-hidden="true">✓</span></button>';
            selectMarkup += '<option value="' + id + '"' + (String(selectedId) === id ? ' selected' : '') + '>' + accEscape(path) + '</option>';
        });
        optionMarkup += '</div></div>';
    } else {
        optionMarkup = '<div class="acc2-fs-picker-empty-group" role="presentation">' + accEscape(label) + '</div>';
    }
    return '<div class="acc2-fs-picker acc2-note-picker" data-fs-picker>' +
        '<button type="button" class="acc2-fs-picker-trigger" data-fs-picker-trigger role="combobox" aria-haspopup="listbox" aria-expanded="false" aria-controls="' + pickerId + '_menu" aria-label="Note for account ' + accEscape(account.account_code) + '"' + (disabled ? ' disabled' : '') + '><span data-fs-picker-value-label>' + accEscape(label) + '</span><span class="acc2-fs-picker-chevron" aria-hidden="true">▾</span></button>' +
        '<div class="acc2-fs-picker-menu" id="' + pickerId + '_menu" data-fs-picker-menu hidden><label class="acc2-fs-picker-search-label" for="' + pickerId + '_search">Find a note</label><input type="search" class="acc2-fs-picker-search" id="' + pickerId + '_search" data-fs-picker-search-input placeholder="Search notes" autocomplete="off"><div class="acc2-fs-picker-option-wrap" data-fs-picker-option-wrap role="listbox" aria-label="Effective notes under ' + accEscape(source ? source.label : 'the assigned category') + '">' + optionMarkup + '<div class="acc2-fs-picker-no-results" data-fs-picker-no-results hidden>No matching notes.</div></div></div>' +
        '<select class="acc2-note-category" data-acc-code="' + accEscape(account.account_code) + '" data-acc-parent="' + (!account.is_leaf ? 'true' : 'false') + '" aria-hidden="true" tabindex="-1" hidden' + (disabled ? ' disabled' : '') + '>' + selectMarkup + '</select></div>';
}
function accRenderNotesRows() {
    const visible = accFilterAccountRows(accAppliedNotesAccountFilter, accAppliedNotesCategoryFilter, accNoteAssignmentSearchText);
    accElement('accNotesCount').textContent = visible.length + ' of ' + accAccounts.length + ' accounts';
    const rows = accElement('accNotesRows');
    if (!visible.length) {
        rows.innerHTML = '<tr><td colspan="6" class="acc2-empty">No accounts match these filters.</td></tr>';
    } else {
        rows.innerHTML = visible.map(function (account) {
            const isParent = !account.is_leaf;
            const statementCategoryId = accEffectiveFinancialCategory(account);
            const noteCategoryId = accEffectiveNoteCategory(account);
            const options = statementCategoryId && statementCategoryId !== '__mixed' ? accGetEffectiveNoteOptions(statementCategoryId) : [];
            const indent = Math.max(0, Number(account.level || 1) - 1) * 16;
            const result = isParent ? '<span class="acc2-parent-help">Applies to children</span>' : '<span class="acc2-saved" data-acc-note-result="' + accEscape(account.account_code) + '">' + (noteCategoryId && noteCategoryId !== '__mixed' && !options.some(function (item) { return String(item.category.id) === String(noteCategoryId); }) ? 'Existing note assignment' : 'Saved') + '</span>';
            return '<tr class="' + (isParent ? 'acc2-parent' : '') + '">' +
                '<td class="acc2-code" style="padding-left:' + (indent + 10) + 'px">' + accEscape(account.account_code) + '</td>' +
                '<td>' + accEscape(account.account_name || 'Unknown account') + '</td>' +
                '<td><span class="acc2-type ' + (isParent ? 'parent' : 'leaf') + '">' + (isParent ? 'Parent' : 'Leaf') + '</span></td>' +
                '<td><span class="acc2-note-source">' + accEscape(accFinancialPickerLabel(statementCategoryId)) + '</span></td>' +
                '<td>' + accNotePickerMarkup(account, noteCategoryId, statementCategoryId, options) + '</td>' +
                '<td>' + result + '</td>' +
                '</tr>';
        }).join('');
    }
    accSetStateReady('accNotesGridState');
    accElement('accNotesGridWrap').hidden = false;
}
function accRenderSource() {
    const upload = accLatestUpload;
    const select = accElement('accTrialBalanceSelect');
    const label = function (item) {
        const start = item.period_start ? String(item.period_start).slice(0, 10) : '';
        const end = item.period_end ? String(item.period_end).slice(0, 10) : (item.as_of_date ? String(item.as_of_date).slice(0, 10) : '');
        const period = start && end ? start + ' to ' + end : (end || 'Period not recorded');
        return period + ' — ' + (item.file_name || 'Trial Balance #' + item.id);
    };
    accAvailableUploads = accTrialBalanceCache.uploads || accAvailableUploads;
    select.innerHTML = accAvailableUploads.length ? accAvailableUploads.map(function (item) {
        return '<option value="' + item.id + '"' + (upload && String(item.id) === String(upload.id) ? ' selected' : '') + '>' + accEscape(label(item)) + '</option>';
    }).join('') : '<option value="">No parsed Trial Balances</option>';
    select.disabled = !accAvailableUploads.length;
}
function accRenderCashflowCategoryList() {
    const list = accElement('accCashflowCategoryList');
    if (!accCashflowCategories.length) { list.innerHTML = '<span class="acc2-empty">No Cash Flow categories defined yet.</span>'; return; }
    list.innerHTML = accCashflowCategories.map(function (category, index) {
        const action = category.is_active ? 'Deactivate' : 'Reactivate';
        return '<div class="acc2-category-item"><div><strong>' + accEscape(category.label) + '</strong><span>' + accEscape(category.category_key) + ' - ' + (category.is_active ? 'Active' : 'Inactive') + '</span></div><div class="acc2-category-actions"><button type="button" class="acc2-mini-action" data-acc-cashflow-category-id="' + category.id + '" data-acc-cashflow-category-action="rename">Rename</button><button type="button" class="acc2-mini-action" data-acc-cashflow-category-id="' + category.id + '" data-acc-cashflow-category-action="up"' + (index === 0 ? ' disabled' : '') + '>Up</button><button type="button" class="acc2-mini-action" data-acc-cashflow-category-id="' + category.id + '" data-acc-cashflow-category-action="down"' + (index === accCashflowCategories.length - 1 ? ' disabled' : '') + '>Down</button><button type="button" class="acc2-mini-action" data-acc-cashflow-category-id="' + category.id + '" data-acc-cashflow-category-action="toggle" data-acc-cashflow-category-active="' + (category.is_active ? 'true' : 'false') + '">' + action + '</button></div></div>';
    }).join('');
}
async function accCreateCashflowCategory(event) {
    event.preventDefault();
    const key = accElement('accCashflowCategoryKey').value.trim();
    const label = accElement('accCashflowCategoryLabel').value.trim();
    const button = event.submitter || accElement('accCashflowCategoryForm').querySelector('button[type="submit"]');
    try {
        await accWithButtonBusy(button, 'Adding category...', async function () {
            await accApi('/api/account-control-center/cashflow/categories', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ client_id: accClientId, category_key: key, label: label, sort_order: accCashflowCategories.length }) });
            accElement('accCashflowCategoryForm').reset();
            await accLoadCashflow();
            accShowStatus('Cash Flow category added.', 'success');
        });
    } catch (error) { accShowStatus(error.message, 'error'); }
}
async function accUpdateCashflowCategory(category, patch, message) {
    await accApi('/api/account-control-center/cashflow/categories/' + category.id, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ client_id: accClientId, label: patch.label == null ? category.label : patch.label, sort_order: patch.sort_order == null ? category.sort_order : patch.sort_order, is_active: patch.is_active == null ? category.is_active : patch.is_active }) });
    await accLoadCashflow();
    accShowStatus(message, 'success');
}
async function accHandleCashflowCategoryAction(event) {
    const button = event.target.closest('[data-acc-cashflow-category-id]');
    if (!button || button.disabled) return;
    const category = accCashflowCategories.find(function (item) { return String(item.id) === button.dataset.accCashflowCategoryId; });
    if (!category) return;
    try {
        const action = button.dataset.accCashflowCategoryAction;
        if (action === 'toggle') {
            await accWithButtonBusy(button, 'Updating category...', function () { return accUpdateCashflowCategory(category, { is_active: button.dataset.accCashflowCategoryActive !== 'true' }, 'Cash Flow category status updated.'); });
            return;
        }
        if (action === 'rename') {
            const label = window.prompt('Display label for ' + category.category_key, category.label);
            if (label && label.trim() && label.trim() !== category.label) await accWithButtonBusy(button, 'Renaming category...', function () { return accUpdateCashflowCategory(category, { label: label.trim() }, 'Cash Flow category renamed.'); });
            return;
        }
        const index = accCashflowCategories.findIndex(function (item) { return item.id === category.id; });
        const target = accCashflowCategories[action === 'up' ? index - 1 : index + 1];
        if (!target) return;
        await accWithButtonBusy(button, 'Reordering categories...', async function () {
            await Promise.all([accUpdateCashflowCategory(category, { sort_order: target.sort_order }, 'Cash Flow category order updated.'), accUpdateCashflowCategory(target, { sort_order: category.sort_order }, 'Cash Flow category order updated.')]);
            await accLoadCashflow();
        });
    } catch (error) { accShowStatus(error.message, 'error'); }
}
function accCashflowAccountMatches(account, search) {
    const haystack = (String(account.account_code || '') + ' ' + String(account.account_name || '')).toLowerCase();
    return !search || haystack.indexOf(search) !== -1;
}
function accCashflowVisibleAccounts(search) {
    if (!search) return accAccounts;
    const visibleCodes = new Set();
    accAccounts.forEach(function (account) {
        if (!accCashflowAccountMatches(account, search)) return;
        const accountCode = accNormaliseCode(account.account_code);
        if (!account.is_leaf) {
            accAccounts.forEach(function (child) {
                const childCode = accNormaliseCode(child.account_code);
                if (accAccountIsWithin(child, accountCode)) visibleCodes.add(childCode);
            });
        }
        let current = account;
        while (current) {
            const code = accNormaliseCode(current.account_code);
            visibleCodes.add(code);
            current = accAccounts.find(function (candidate) { return accNormaliseCode(candidate.account_code) === accNormaliseCode(current.parent_account_code); });
        }
    });
    return accAccounts.filter(function (account) { return visibleCodes.has(accNormaliseCode(account.account_code)); });
}
function accRenderCashflowSelected(type) {
    const panel = accElement(type === 'cash' ? 'accCashflowCashSelected' : 'accCashflowBankSelected');
    const selected = new Set((accCashflowTreasury[type] || []).map(accNormaliseCode));
    const accounts = accAccounts.filter(function (account) { return selected.has(accNormaliseCode(account.account_code)); });
    panel.innerHTML = '<div class="acc2-treasury-selected-head"><strong>Selected ' + (type === 'cash' ? 'Cash' : 'Bank') + '</strong><span>' + accounts.length + '</span></div>' + (accounts.length ? accounts.map(function (account) {
        const code = accNormaliseCode(account.account_code);
        return '<div class="acc2-selected-account"><div><strong>' + accEscape(code) + '</strong><small>' + accEscape(account.account_name || '') + '</small></div><button type="button" data-acc-treasury-remove-type="' + type + '" data-acc-treasury-remove-code="' + accEscape(code) + '" aria-label="Remove ' + accEscape(code) + '">&times;</button></div>';
    }).join('') : '<p class="acc2-treasury-empty">No accounts selected.</p>');
}
function accRenderCashflowAccountOptions() {
    ['cash', 'bank'].forEach(function (type) {
        const searchId = type === 'cash' ? 'accCashflowCashSearch' : 'accCashflowBankSearch';
        const container = accElement(type === 'cash' ? 'accCashflowCashOptions' : 'accCashflowBankOptions');
        const search = String(accElement(searchId).value || '').trim().toLowerCase();
        const selected = new Set((accCashflowTreasury[type] || []).map(accNormaliseCode));
        const visible = accCashflowVisibleAccounts(search);
        container.innerHTML = visible.length ? visible.map(function (account) {
            const code = accNormaliseCode(account.account_code);
            const isParent = !account.is_leaf;
            return '<label class="acc2-account-option ' + (isParent ? 'acc2-account-parent' : '') + '"><input type="checkbox" data-acc-treasury-type="' + type + '" data-acc-treasury-code="' + accEscape(code) + '"' + (selected.has(code) ? ' checked' : '') + (isParent || !accIsAdmin ? ' disabled' : '') + '><span style="padding-left:' + (Math.max(0, Number(account.level || 1) - 1) * 16) + 'px"><strong>' + accEscape(code) + '</strong><small>' + accEscape(account.account_name || '') + '</small></span><em>' + (isParent ? 'Main class' : 'Select') + '</em></label>';
        }).join('') : '<span class="acc2-empty">No matching Trial Balance accounts.</span>';
        accRenderCashflowSelected(type);
    });
}
function accUpdateCashflowTreasurySelection(event) {
    const checkbox = event.target.closest('[data-acc-treasury-type]');
    if (!checkbox) return;
    const type = checkbox.dataset.accTreasuryType;
    const code = checkbox.dataset.accTreasuryCode;
    const other = type === 'cash' ? 'bank' : 'cash';
    if (checkbox.checked) {
        accCashflowTreasury[type] = Array.from(new Set((accCashflowTreasury[type] || []).concat([code])));
        accCashflowTreasury[other] = (accCashflowTreasury[other] || []).filter(function (item) { return accNormaliseCode(item) !== code; });
    } else {
        accCashflowTreasury[type] = (accCashflowTreasury[type] || []).filter(function (item) { return accNormaliseCode(item) !== code; });
    }
    accRenderCashflowAccountOptions();
}
function accRemoveCashflowTreasurySelection(event) {
    const button = event.target.closest('[data-acc-treasury-remove-type]');
    if (!button) return;
    const type = button.dataset.accTreasuryRemoveType;
    const code = button.dataset.accTreasuryRemoveCode;
    accCashflowTreasury[type] = (accCashflowTreasury[type] || []).filter(function (item) { return accNormaliseCode(item) !== code; });
    accRenderCashflowAccountOptions();
}
async function accSaveCashflowTreasury(button) {
    await accWithButtonBusy(button, 'Saving Cash & Bank accounts...', async function () {
        await accApi('/api/account-control-center/cashflow/treasury-accounts', { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(Object.assign({ client_id: accClientId, cash_account_codes: accCashflowTreasury.cash || [], bank_account_codes: accCashflowTreasury.bank || [] }, accSelectedUploadPayload())) });
        accShowStatus('Cash and Bank account selections saved.', 'success');
    });
}
async function accSaveCashflowSettings(button) {
    await accWithButtonBusy(button, 'Saving Cash Flow settings...', async function () {
        const roleCategories = accCollectCashflowComboboxCategories();
        const payload = { client_id: accClientId, match_internal_transfers: accElement('accCashflowMatchTransfers').checked, receipt_presentation: accElement('accCashflowReceiptPresentation').value, customer_receipt_category_ids: roleCategories.customer_receipt, bank_charge_category_ids: roleCategories.bank_charge, withholding_tax_category_ids: roleCategories.withholding_tax, other_deduction_category_ids: roleCategories.other_deduction };
        const result = await accApi('/api/account-control-center/cashflow/settings', { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) });
        accCashflowSettings = Object.assign({}, accCashflowSettings, { match_internal_transfers: payload.match_internal_transfers, receipt_presentation: payload.receipt_presentation, role_categories: result.role_categories || roleCategories });
        accRenderCashflowComboboxCategories();
        accShowStatus('Cash Flow transfer and customer receipt settings saved.', 'success');
    });
}
function accInvalidateTrialBalanceCache() {
    if (accTrialBalanceCache.clientId === String(accClientId || '')) {
        accTrialBalanceCache.accounts = null;
        accTrialBalanceCache.upload = null;
        accTrialBalanceCache.uploads = null;
    }
}
async function accLoadTrialBalance(forceRefresh) {
    const clientId = String(accClientId || '');
    const uploadId = String(accSelectedUploadId || '');
    if (!forceRefresh && accTrialBalanceCache.clientId === clientId && accTrialBalanceCache.uploadId === uploadId && accTrialBalanceCache.accounts) {
        accAccounts = accTrialBalanceCache.accounts;
        accLatestUpload = accTrialBalanceCache.upload;
        return;
    }
    if (accTrialBalanceCache.request && accTrialBalanceCache.clientId === clientId && accTrialBalanceCache.uploadId === uploadId) return accTrialBalanceCache.request;
    accTrialBalanceCache.clientId = clientId;
    accTrialBalanceCache.uploadId = uploadId;
    const request = accApi('/api/account-control-center/mapping?client_id=' + encodeURIComponent(accClientId) + (uploadId ? '&trial_balance_upload_id=' + encodeURIComponent(uploadId) : '')).then(function (result) {
        accTrialBalanceCache.accounts = result.accounts || [];
        accTrialBalanceCache.upload = result.upload || null;
        accTrialBalanceCache.uploads = result.uploads || [];
        accAvailableUploads = accTrialBalanceCache.uploads;
        accSelectedUploadId = result.upload ? String(result.upload.id) : null;
        accTrialBalanceCache.uploadId = String(accSelectedUploadId || '');
        accAccounts = accTrialBalanceCache.accounts;
        accLatestUpload = accTrialBalanceCache.upload;
        return result;
    }).finally(function () {
        if (accTrialBalanceCache.request === request) accTrialBalanceCache.request = null;
    });
    accTrialBalanceCache.request = request;
    return request;
}
async function accLoadMapping(forceRefresh) {
    if (forceRefresh) accInvalidateTrialBalanceCache();
    await accLoadTrialBalance(!!forceRefresh);
    accRenderSource();
    accRenderRows();
    if (accCashflowLoaded) accRenderCashflowRows();
    if (accFinancialLoaded) accRenderFinancialRows();
    if (accNotesLoaded) accRenderNotesRows();
}
function accSetCashflowTemplateState(message, isError) {
    const state = accElement('accCashflowTemplateMappingState');
    state.textContent = message || '';
    state.className = 'acc2-grid-state' + (isError ? ' error' : '');
    state.hidden = !message;
    accElement('accCashflowTemplateMapping').hidden = true;
    accElement('accCashflowTemplateSave').disabled = true;
}
function accCashflowTemplateOptions(sources) {
    const byTemplate = new Map();
    (sources || []).forEach(function (source) {
        const templateId = Number(source.template_id || 0);
        if (!templateId || byTemplate.has(templateId)) return;
        byTemplate.set(templateId, source);
    });
    return Array.from(byTemplate.values());
}
function accRenderCashflowTemplateSources(sources) {
    const select = accElement('accCashflowTemplateSelect');
    const options = accCashflowTemplateOptions(sources);
    if (!options.length) {
        select.innerHTML = '<option value="">No Client Data Bank templates available</option>';
        select.disabled = true;
        accSetCashflowTemplateState('Upload or select a Client Data Bank template before mapping Cashflows columns.');
        return options;
    }
    select.disabled = false;
    select.innerHTML = '<option value="">Choose a template</option>' + options.map(function (source) {
        const label = source.template_name || ('Template ' + source.template_id);
        return '<option value="' + accEscape(source.template_id) + '">' + accEscape(label) + '</option>';
    }).join('');
    const selected = String(accCashflowSettings.source_template_id || options[0].template_id);
    select.value = options.some(function (source) { return String(source.template_id) === selected; }) ? selected : '';
    accElement('accCashflowTemplateHint').textContent = options.length + ' template' + (options.length === 1 ? '' : 's') + ' available from Client Data Bank.';
    return options;
}
function accRenderCashflowTemplateMapping(mapping) {
    accCashflowTemplateMapping = mapping;
    const state = accElement('accCashflowTemplateMappingState');
    const container = accElement('accCashflowTemplateMapping');
    const save = accElement('accCashflowTemplateSave');
    const columns = mapping.columns || [];
    container.innerHTML = '<div class="acc2-template-mapping-head"><span>Cashflows field</span><span>Template column</span></div>' + (mapping.roles || []).map(function (role) {
        const selected = role.template_column_id == null ? '' : String(role.template_column_id);
        const required = role.required ? '<span class="acc2-template-required">Required</span>' : '<span class="acc2-template-recommended">Recommended</span>';
        const options = '<option value="">Not mapped</option>' + columns.map(function (column) {
            return '<option value="' + accEscape(column.template_column_id) + '"' + (String(column.template_column_id) === selected ? ' selected' : '') + '>' + accEscape(column.column_name) + '</option>';
        }).join('');
        return '<label class="acc2-template-mapping-row"><span><strong>' + accEscape(role.label) + '</strong>' + required + '</span><select class="acc2-template-role" data-acc-template-role="' + accEscape(role.role) + '">' + options + '</select></label>';
    }).join('');
    state.hidden = true;
    container.hidden = false;
    save.disabled = !(mapping.ready && (mapping.roles || []).some(function (role) { return role.required; }));
    if (!mapping.ready) {
        save.disabled = true;
        state.textContent = 'Map all required fields before saving.';
        state.className = 'acc2-grid-state error';
        state.hidden = false;
    }
}
function accCashflowTemplateMappingIsReady() {
    const requiredRoles = (accCashflowTemplateMapping && accCashflowTemplateMapping.roles || []).filter(function (role) { return role.required; }).map(function (role) { return role.role; });
    return requiredRoles.every(function (role) {
        const select = accElement('accCashflowTemplateMapping').querySelector('[data-acc-template-role="' + CSS.escape(role) + '"]');
        return select && select.value;
    });
}
function accHandleCashflowTemplateMappingChange() {
    accElement('accCashflowTemplateSave').disabled = !accCashflowTemplateMappingIsReady();
}
async function accLoadCashflowTemplateMapping(templateId) {
    if (!templateId) {
        accCashflowTemplateMapping = null;
        accSetCashflowTemplateState('Select a template to view its columns.');
        return;
    }
    accSetCashflowTemplateState('Loading template columns...');
    const select = accElement('accCashflowTemplateSelect');
    select.disabled = true;
    select.setAttribute('aria-busy', 'true');
    try {
        const mapping = await accWithBusy('Loading Cash Flow template columns...', function () { return accApi('/api/reconciliation/cashflows/template-columns?client_id=' + encodeURIComponent(accClientId) + '&template_id=' + encodeURIComponent(templateId)); });
        accRenderCashflowTemplateMapping(mapping);
    } finally {
        select.disabled = false;
        select.setAttribute('aria-busy', 'false');
    }
}
async function accOpenCashflowTemplateModal() {
    accSetCashflowModal('accCashflowTemplateModal', true);
    accElement('accCashflowTemplateSelect').disabled = true;
    accElement('accCashflowTemplateSelect').innerHTML = '<option value="">Loading templates...</option>';
    accSetCashflowTemplateState('Loading Client Data Bank templates...');
    try {
        await accWithBusy('Loading Cash Flow templates...', async function () {
            const result = await accApi('/api/reconciliation/cashflows/sources?client_id=' + encodeURIComponent(accClientId));
            const options = accRenderCashflowTemplateSources(result.sources || []);
            const selected = accElement('accCashflowTemplateSelect').value;
            if (selected && options.length) await accLoadCashflowTemplateMapping(selected);
        });
    } catch (error) {
        accSetCashflowTemplateState(error.message, true);
    }
}
async function accSaveCashflowTemplate() {
    const templateId = Number(accElement('accCashflowTemplateSelect').value || 0);
    if (!templateId || !accCashflowTemplateMappingIsReady()) return;
    const button = accElement('accCashflowTemplateSave');
    await accWithButtonBusy(button, 'Saving template mapping...', async function () {
        const mappings = Array.from(accElement('accCashflowTemplateMapping').querySelectorAll('[data-acc-template-role]')).filter(function (select) { return select.value; }).map(function (select) {
            return { role: select.dataset.accTemplateRole, template_column_id: Number(select.value) };
        });
        const result = await accApi('/api/reconciliation/cashflows/template-columns', { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ client_id: accClientId, template_id: templateId, mappings: mappings, set_as_default: true }) });
        accCashflowSettings.source_template_id = templateId;
        accRenderCashflowTemplateMapping(result);
        accShowStatus('Cashflows transaction template mapping saved.', 'success');
    });
}
async function accLoadCashflow() {
    await accLoadTrialBalance(false);
    const result = await accApi('/api/account-control-center/cashflow/setup?client_id=' + encodeURIComponent(accClientId) + accSelectedUploadQuery());
    const setupAccounts = new Map((result.accounts || []).map(function (account) {
        return [accNormaliseCode(account.account_code), account];
    }));
    accAccounts.forEach(function (account) {
        const setupAccount = setupAccounts.get(accNormaliseCode(account.account_code));
        if (setupAccount && Object.prototype.hasOwnProperty.call(setupAccount, 'category_id')) account.category_id = setupAccount.category_id;
    });
    accCashflowCategories = result.categories || [];
    accCashflowHistoricalOrphans = result.historical_orphans || [];
    accCashflowTreasury = result.treasury_accounts || { cash: [], bank: [] };
    accCashflowSettings = result.settings || { match_internal_transfers: false, receipt_presentation: 'net', source_template_id: null, role_categories: { customer_receipt: [], bank_charge: [], withholding_tax: [], other_deduction: [] } };
    accCashflowLoaded = true;
    accRenderCashflowRows();
    accRenderCashflowCategoryList();
    accRenderCashflowAccountOptions();
    accRenderCashflowComboboxCategories();
    accElement('accCashflowMatchTransfers').checked = !!accCashflowSettings.match_internal_transfers;
    accElement('accCashflowReceiptPresentation').value = accCashflowSettings.receipt_presentation || 'net';
}
async function accLoadFinancialCategories() {
    const results = await Promise.all([
        accApi('/api/account-control-center/categories?client_id=' + encodeURIComponent(accClientId) + '&category_type=financial_statement&include_inactive=true'),
        accApi('/api/account-control-center/statement-calculations?client_id=' + encodeURIComponent(accClientId) + '&statement_section=balance_sheet'),
        accApi('/api/account-control-center/statement-calculations?client_id=' + encodeURIComponent(accClientId) + '&statement_section=income_statement'),
    ]);
    const result = results[0];
    accFinancialCategories = result.categories || [];
    accFinancialCalculations = results.slice(1).flatMap(function (item) { return item.calculations || []; });
    accFinancialLoaded = true;
    accRenderFinancialRows();
    if (accNotesLoaded) accRenderNotesRows();
    accRenderFinancialOutlines();
    accRenderNotesOutlines();
}
function accRenderFinancialOutlines() {
    document.querySelectorAll('[data-fs-section]').forEach(function (panel) {
        const section = panel.dataset.fsSection;
        const readOnly = accElement('accFinancialSetupModal').dataset.entryMode === 'notes';
        const rows = accFinancialCategories.filter(function (item) { return item.statement_section === section; });
        const headings = rows.filter(function (item) { return item.row_type === 'heading'; });
        const leaves = rows.filter(function (item) { return item.row_type !== 'heading'; });
        const calculations = accFinancialCalculations.filter(function (item) { return item.statement_section === section; });
        const totals = calculations.filter(function (item) { return item.calculation_type === 'total'; }).sort(function (a, b) { return Number(a.sort_order) - Number(b.sort_order) || Number(a.id) - Number(b.id); });
        const totalsAfterSourceRow = new Map();
        const unplacedTotals = [];
        function actions(item) { return readOnly ? '' : '<button type="button" class="acc2-fs-small" data-fs-action="delete" data-fs-id="' + item.id + '">Delete</button><button type="button" class="acc2-fs-small" data-fs-action="rename" data-fs-id="' + item.id + '">Rename</button><button type="button" class="acc2-fs-small" data-fs-action="toggle" data-fs-id="' + item.id + '" data-fs-active="' + item.is_active + '">' + (item.is_active ? 'Deactivate' : 'Restore') + '</button>'; }
        function dragHandle(item, kind) { return readOnly ? '' : '<button type="button" class="acc2-fs-drag-handle" draggable="true" data-fs-drag-handle data-fs-id="' + item.id + '" aria-label="Drag to reorder ' + kind + ' ' + accEscape(item.label) + '" title="Drag to reorder">☰</button>'; }
        function calculationMarkup(calculation, kind) {
            const summary = (calculation.components || []).map(function (component, index) {
                return (component.operator === 'subtract' ? (index ? ' − ' : '− ') : (index ? ' + ' : '+ ')) + accEscape(component.label);
            }).join('');
            const actionsMarkup = readOnly ? '' : '<span><button type="button" class="acc2-fs-small" data-fs-calc-action="edit" data-fs-calc-id="' + calculation.id + '">Edit</button><button type="button" class="acc2-fs-small" data-fs-calc-action="delete" data-fs-calc-id="' + calculation.id + '">Delete</button></span>';
            return '<div class="acc2-fs-calculation-line ' + kind + '" data-fs-calculation-id="' + calculation.id + '"><span class="acc2-fs-calculation-label"><span class="acc2-fs-sigma" aria-hidden="true">Σ</span><span><strong>' + accEscape(calculation.label) + '</strong><small>' + (summary || 'No rows selected') + '</small></span></span>' + actionsMarkup + '</div>';
        }
        totals.forEach(function (total) {
            const anchor = accFinancialTotalAnchorRow(total.components || [], headings, calculations, accFinancialCategories);
            if (!anchor) { unplacedTotals.push(total); return; }
            const group = totalsAfterSourceRow.get(anchor.key) || [];
            group.push(total);
            totalsAfterSourceRow.set(anchor.key, group);
        });
        function totalsMarkupAfter(key) {
            return (totalsAfterSourceRow.get(key) || []).map(function (total) {
                return calculationMarkup(total, 'total') + totalsMarkupAfter('calculation:' + total.id);
            }).join('');
        }
        const outline = headings.map(function (heading) {
            const children = leaves.filter(function (item) { return Number(item.parent_category_id) === Number(heading.id); });
            const childRows = children.map(function (item) { return '<div class="acc2-fs-category acc2-fs-sort-row' + (item.is_active ? '' : ' inactive') + '" data-fs-sort-row data-fs-sort-kind="category" data-fs-sort-group="children:' + heading.id + '" data-fs-id="' + item.id + '"><span class="acc2-fs-category-label">' + dragHandle(item, 'category') + '<span>' + accEscape(item.label) + '</span></span><span>' + actions(item) + '</span></div>' + totalsMarkupAfter('category:' + item.id); }).join('');
            const subtotal = calculations.find(function (item) { return item.calculation_type === 'subtotal' && Number(item.parent_heading_id) === Number(heading.id); });
            const subtotalButton = !readOnly && !subtotal && children.length ? '<button type="button" class="acc2-fs-small primary" data-fs-calc-action="add-subtotal" data-fs-heading-id="' + heading.id + '">+ Subtotal</button>' : '';
            const addCategoryButton = readOnly ? '' : '<button type="button" class="acc2-fs-small primary" data-fs-action="add-category" data-fs-parent="' + heading.id + '">+ Category</button>';
            const subtotalRow = subtotal ? calculationMarkup(subtotal, 'subtotal') : '';
            const subtotalTotalRows = subtotal ? totalsMarkupAfter('calculation:' + subtotal.id) : '';
            return '<article class="acc2-fs-heading acc2-fs-sort-row' + (heading.is_active ? '' : ' inactive') + '" data-fs-sort-row data-fs-sort-kind="heading" data-fs-sort-group="headings:' + accEscape(section) + '" data-fs-id="' + heading.id + '"><div class="acc2-fs-heading-title"><span class="acc2-fs-heading-label">' + dragHandle(heading, 'heading') + '<strong>' + accEscape(heading.label) + '</strong></span><div>' + actions(heading) + subtotalButton + addCategoryButton + '</div></div><div class="acc2-fs-children" data-fs-sort-group="children:' + heading.id + '">' + (childRows || '<div class="acc2-fs-empty">No categories in this heading yet.</div>') + subtotalRow + subtotalTotalRows + '</div></article>';
        }).join('');
        const standalone = leaves.filter(function (item) { return !item.parent_category_id; });
        const unplaced = section === 'balance_sheet' ? accFinancialCategories.filter(function (item) { return !item.statement_section && item.row_type !== 'heading'; }) : [];
        const standaloneRows = standalone.map(function (item) { return '<div class="acc2-fs-category acc2-fs-sort-row' + (item.is_active ? '' : ' inactive') + '" data-fs-sort-row data-fs-sort-kind="category" data-fs-sort-group="standalone:' + accEscape(section) + '" data-fs-id="' + item.id + '"><span class="acc2-fs-category-label">' + dragHandle(item, 'category') + '<span>' + accEscape(item.label) + '</span></span><span>' + actions(item) + '</span></div>' + totalsMarkupAfter('category:' + item.id); }).join('');
        const standaloneMarkup = standalone.length ? '<div class="acc2-fs-standalone acc2-fs-sort-list"><h3>Standalone categories</h3><div data-fs-sort-group="standalone:' + accEscape(section) + '">' + standaloneRows + '</div></div>' : '';
        const unplacedRows = unplaced.map(function (item) { return '<div class="acc2-fs-category acc2-fs-sort-row' + (item.is_active ? '' : ' inactive') + '" data-fs-sort-row data-fs-sort-kind="category" data-fs-sort-group="unplaced" data-fs-id="' + item.id + '"><span class="acc2-fs-category-label">' + dragHandle(item, 'category') + '<span>' + accEscape(item.label) + '</span></span><span>' + actions(item) + '</span></div>'; }).join('');
        const toolbarActions = readOnly ? '' : '<div><button type="button" class="acc2-action-btn" data-fs-action="add-heading">+ Add heading</button><button type="button" class="acc2-action-btn secondary" data-fs-action="add-category">+ Add category</button><button type="button" class="acc2-action-btn secondary" data-fs-calc-action="add-total">+ Add total</button></div>';
        const toolbarMessage = readOnly ? 'Read-only reference. Edit statement structure from Financial Statement accounts.' : 'Drag the ☰ handle to reorder within a statement or parent.';
        const emptyMessage = readOnly ? 'No categories are available in this statement yet.' : 'Start with a heading, or add a standalone category.';
        const unplacedTotalMarkup = !readOnly && unplacedTotals.length ? '<section class="acc2-fs-total-list"><h3>Totals needing a source heading</h3>' + unplacedTotals.map(function (item) { return calculationMarkup(item, 'total'); }).join('') + '</section>' : '';
        panel.innerHTML = accFinancialCalculationEditorMarkup(section) + '<div class="acc2-fs-toolbar"><span>' + toolbarMessage + '</span><span class="acc2-fs-order-status" data-fs-order-status role="status" aria-live="polite"></span>' + toolbarActions + '</div>' + (outline ? '<div class="acc2-fs-heading-list" data-fs-sort-group="headings:' + accEscape(section) + '">' + outline + '</div>' : '') + standaloneMarkup + (unplaced.length ? '<div class="acc2-fs-standalone acc2-fs-unplaced acc2-fs-sort-list"><h3>Unplaced categories</h3><div data-fs-sort-group="unplaced">' + unplacedRows + '</div></div>' : '') + unplacedTotalMarkup + (!outline && !standalone.length && !unplaced.length ? '<div class="acc2-fs-empty-state">' + emptyMessage + '</div>' : '');
        accUpdateFinancialTotalChoiceAvailability(panel.querySelector('[data-fs-calc-editor]'));
    });
}

function accFinancialTotalAnchorRow(components, headings, calculations, categories) {
    const totals = calculations.filter(function (item) { return item.calculation_type === 'total'; })
        .sort(function (a, b) { return Number(a.sort_order) - Number(b.sort_order) || Number(a.id) - Number(b.id); });
    const sources = (components || []).map(function (component) {
        if (component.source_category_id != null) {
            const category = categories.find(function (item) { return Number(item.id) === Number(component.source_category_id) && item.row_type === 'category'; });
            return category ? { key: 'category:' + category.id, section: category.statement_section } : null;
        }
        if (component.source_calculation_id != null) {
            const calculation = calculations.find(function (item) { return Number(item.id) === Number(component.source_calculation_id) && ['subtotal', 'total'].includes(item.calculation_type); });
            return calculation ? { key: 'calculation:' + calculation.id, section: calculation.statement_section } : null;
        }
        return null;
    }).filter(Boolean);
    if (!sources.length) return null;
    const section = sources[0].section;
    const rowPositions = new Map();
    headings.forEach(function (heading, headingIndex) {
        const children = categories.filter(function (item) {
            return item.statement_section === section && item.row_type === 'category' && Number(item.parent_category_id) === Number(heading.id);
        });
        const base = headingIndex * 100000;
        children.forEach(function (category, childIndex) {
            rowPositions.set('category:' + category.id, { order: base + (childIndex + 1) * 10, label: category.label });
        });
        const subtotal = calculations.find(function (item) {
            return item.statement_section === section && item.calculation_type === 'subtotal' && Number(item.parent_heading_id) === Number(heading.id);
        });
        if (subtotal) rowPositions.set('calculation:' + subtotal.id, { order: base + (children.length + 1) * 10, label: subtotal.label });
    });
    const standalone = categories.filter(function (item) {
        return item.statement_section === section && item.row_type === 'category' && item.parent_category_id == null;
    });
    const standaloneBase = headings.length * 100000;
    standalone.forEach(function (category, index) {
        rowPositions.set('category:' + category.id, { order: standaloneBase + (index + 1) * 10, label: category.label });
    });
    const totalAnchorCounts = new Map();
    totals.forEach(function (total) {
        const anchorSources = (total.components || []).map(function (component) {
            if (component.source_category_id != null) return 'category:' + component.source_category_id;
            if (component.source_calculation_id != null) return 'calculation:' + component.source_calculation_id;
            return null;
        }).filter(Boolean);
        const anchor = anchorSources.map(function (key) {
            const row = rowPositions.get(key);
            return row ? { key: key, ...row } : null;
        }).filter(Boolean).sort(function (a, b) { return b.order - a.order; })[0];
        if (!anchor) return;
        const count = totalAnchorCounts.get(anchor.key) || 0;
        totalAnchorCounts.set(anchor.key, count + 1);
        rowPositions.set('calculation:' + total.id, { order: anchor.order + 1 + count, label: total.label });
    });
    let anchor = null;
    sources.forEach(function (source) {
        const row = rowPositions.get(source.key);
        if (row && (!anchor || row.order > anchor.order)) anchor = { key: source.key, label: row.label, order: row.order };
    });
    return anchor;
}

function accFinancialCalculationEditorMarkup(section) {
    const state = accFinancialCalculationEditor;
    if (!state || section !== state.statement_section || accElement('accFinancialSetupModal').dataset.entryMode === 'notes') return '';
    const calculations = accFinancialCalculations.filter(function (item) { return item.statement_section === section; });
    const heading = accFinancialCategories.find(function (item) { return Number(item.id) === Number(state.parent_heading_id); });
    const selected = new Map((state.components || []).map(function (component) {
        const key = component.source_category_id != null ? 'category:' + component.source_category_id : 'calculation:' + component.source_calculation_id;
        return [key, component];
    }));
    const choices = state.calculation_type === 'subtotal'
        ? accFinancialCategories.filter(function (item) { return item.statement_section === section && item.row_type === 'category' && Number(item.parent_category_id) === Number(state.parent_heading_id); }).map(function (item) { return { id: item.id, label: item.label, kind: 'category', headingId: state.parent_heading_id }; })
        : [];
    const totalGroups = state.calculation_type === 'total'
        ? accFinancialCategories.filter(function (item) { return item.statement_section === section && item.row_type === 'heading'; }).map(function (item) {
            const children = accFinancialCategories.filter(function (category) { return category.statement_section === section && category.row_type === 'category' && Number(category.parent_category_id) === Number(item.id); });
            if (!children.length) return null;
            const subtotal = calculations.find(function (calculation) { return calculation.calculation_type === 'subtotal' && Number(calculation.parent_heading_id) === Number(item.id); });
            const rows = (subtotal ? [{ id: subtotal.id, label: subtotal.label, kind: 'calculation', headingId: item.id }] : []).concat(children.map(function (category) {
                return { id: category.id, label: category.label, kind: 'category', headingId: item.id };
            }));
            return { heading: item, choices: rows };
        }).filter(Boolean)
        : [];
    if (state.calculation_type === 'total' && section === 'income_statement') {
        const standaloneChoices = accFinancialCategories.filter(function (item) {
            return item.statement_section === section && item.row_type === 'category' && !item.parent_category_id;
        }).map(function (item) { return { id: item.id, label: item.label, kind: 'category', headingId: 'standalone' }; });
        if (standaloneChoices.length) totalGroups.push({ heading: { id: 'standalone', label: 'Standalone categories' }, choices: standaloneChoices });
    }
    if (state.calculation_type === 'total') {
        const earlierTotals = calculations.filter(function (calculation) {
            return calculation.calculation_type === 'total'
                && Number(calculation.id) !== Number(state.calculation_id)
                && Number(calculation.sort_order) < Number(state.sort_order);
        }).map(function (calculation) { return { id: calculation.id, label: calculation.label, kind: 'total', headingId: 'earlier-totals' }; });
        if (earlierTotals.length) totalGroups.unshift({ heading: { id: 'earlier-totals', label: 'Earlier totals' }, choices: earlierTotals });
    }
    const allChoices = state.calculation_type === 'total' ? totalGroups.flatMap(function (group) { return group.choices; }) : choices;
    function renderChoice(choice) {
        const key = choice.kind + ':' + choice.id;
        const component = selected.get(key);
        const totalChoice = state.calculation_type === 'total';
        const checked = !totalChoice || !!component;
        const sourceAttr = choice.kind === 'category' ? 'data-source-category-id="' : 'data-source-calculation-id="';
        const choiceKind = choice.kind === 'category' ? 'category' : choice.kind === 'total' ? 'total' : 'subtotal';
        const kindLabel = totalChoice && choiceKind !== 'category' ? '<small class="acc2-fs-calc-choice-type">' + (choiceKind === 'total' ? 'Total' : 'Subtotal') + '</small>' : '';
        return '<label class="acc2-fs-calc-choice ' + (totalChoice ? 'is-' + choiceKind : '') + '" data-fs-choice-row data-fs-choice-heading-id="' + choice.headingId + '" data-fs-choice-kind="' + choiceKind + '"><span><input type="' + (totalChoice ? 'checkbox' : 'hidden') + '" data-fs-calc-include data-fs-choice-heading-id="' + choice.headingId + '" data-fs-choice-kind="' + choiceKind + '" ' + (checked ? 'checked ' : '') + sourceAttr + choice.id + '"><span class="acc2-fs-calc-choice-name">' + accEscape(choice.label) + '</span>' + kindLabel + '<small data-fs-choice-overlap hidden></small></span><select data-fs-calc-operator aria-label="Add or subtract ' + accEscape(choice.label) + '"' + (totalChoice && !checked ? ' disabled' : '') + '><option value="add"' + (!component || component.operator === 'add' ? ' selected' : '') + '>+</option><option value="subtract"' + (component && component.operator === 'subtract' ? ' selected' : '') + '>−</option></select></label>';
    }
    const rows = state.calculation_type === 'total'
        ? totalGroups.map(function (group) { return '<section class="acc2-fs-calc-choice-group" data-fs-choice-group="' + group.heading.id + '"><h4>' + accEscape(group.heading.label) + '</h4>' + group.choices.map(renderChoice).join('') + '</section>'; }).join('')
        : choices.map(renderChoice).join('');
    const title = state.calculation_type === 'subtotal' ? 'Subtotal for ' + (heading ? heading.label : 'heading') : 'Statement total';
    const initialAnchor = state.calculation_type === 'total'
        ? accFinancialTotalAnchorRow(state.components || [], accFinancialCategories.filter(function (item) { return item.statement_section === section && item.row_type === 'heading'; }), calculations, accFinancialCategories)
        : null;
    const placementPreview = state.calculation_type === 'total'
        ? '<small class="acc2-fs-calc-placement" data-fs-calc-placement role="status">' + (initialAnchor ? 'Appears after: ' + accEscape(initialAnchor.label) : 'Select rows to choose where this total appears.') + '</small>'
        : '';
    return '<section class="acc2-fs-calc-editor" data-fs-calc-editor><div class="acc2-fs-calc-editor-head"><div><strong>' + accEscape(title) + '</strong><small>' + (state.calculation_type === 'subtotal' ? 'All categories under this heading are included.' : 'Choose a subtotal or individual categories within each heading.') + '</small>' + placementPreview + '</div></div><label class="acc2-fs-calc-label">Line label<input type="text" maxlength="120" data-fs-calc-label value="' + accEscape(state.label || '') + '"></label><div class="acc2-fs-calc-choices">' + (rows || '<div class="acc2-fs-empty">' + (state.calculation_type === 'subtotal' ? 'Add a category to this heading first.' : 'Add ' + (section === 'income_statement' ? 'Income Statement' : 'Balance Sheet') + ' categories before creating a total.')) + '</div><div class="acc2-fs-calc-editor-actions"><button type="button" class="acc2-action-btn" data-fs-calc-action="save"' + (!allChoices.length ? ' disabled' : '') + '>Save line</button><button type="button" class="acc2-action-btn secondary" data-fs-calc-action="cancel">Cancel</button></div></section>';
}
function accUpdateFinancialCalculationPlacementPreview(editor) {
    const preview = editor && editor.querySelector('[data-fs-calc-placement]');
    if (!preview) return;
    const components = Array.from(editor.querySelectorAll('[data-fs-calc-include]')).filter(function (input) {
        return input.type === 'hidden' || input.checked;
    }).map(function (input) { return {
        source_category_id: input.dataset.sourceCategoryId || null,
        source_calculation_id: input.dataset.sourceCalculationId || null,
    }; });
    const section = accFinancialCalculationEditor ? accFinancialCalculationEditor.statement_section : 'balance_sheet';
    const headings = accFinancialCategories.filter(function (item) { return item.statement_section === section && item.row_type === 'heading'; });
    const calculations = accFinancialCalculations.filter(function (item) { return item.statement_section === section; });
    const anchor = accFinancialTotalAnchorRow(components, headings, calculations, accFinancialCategories);
    preview.textContent = anchor ? 'Appears after: ' + anchor.label : 'Select rows to choose where this total appears.';
}
function accUpdateFinancialTotalChoiceAvailability(editor) {
    if (!editor) return;
    function lineageFor(kind, id, visiting) {
        const key = kind + ':' + id;
        if (kind === 'category') return new Set([key]);
        const calculation = accFinancialCalculations.find(function (item) { return Number(item.id) === Number(id); });
        if (!calculation) return new Set();
        visiting = visiting || new Set();
        if (visiting.has(Number(id))) return new Set();
        visiting.add(Number(id));
        const lineage = new Set();
        (calculation.components || []).forEach(function (component) {
            if (component.source_category_id != null) lineage.add('category:' + component.source_category_id);
            else if (component.source_calculation_id != null) {
                const nested = accFinancialCalculations.find(function (item) { return Number(item.id) === Number(component.source_calculation_id); });
                const nestedKind = nested && nested.calculation_type;
                lineageFor(nestedKind, component.source_calculation_id, visiting).forEach(function (source) { lineage.add(source); });
            }
        });
        visiting.delete(Number(id));
        return lineage;
    }
    function inputLineage(input) {
        if (input.dataset.sourceCategoryId) return lineageFor('category', input.dataset.sourceCategoryId);
        if (input.dataset.sourceCalculationId) {
            const source = accFinancialCalculations.find(function (item) { return Number(item.id) === Number(input.dataset.sourceCalculationId); });
            return lineageFor(source && source.calculation_type, input.dataset.sourceCalculationId);
        }
        return new Set();
    }
    const selected = Array.from(editor.querySelectorAll('[data-fs-calc-include][type="checkbox"]')).filter(function (input) { return input.checked; }).map(function (input) {
        return { input: input, lineage: inputLineage(input), label: input.closest('[data-fs-choice-row]').querySelector('.acc2-fs-calc-choice-name').textContent.trim(), kind: input.dataset.fsChoiceKind };
    });
    const groups = new Map();
    editor.querySelectorAll('[data-fs-calc-include][type="checkbox"]').forEach(function (input) {
        const key = input.dataset.fsChoiceHeadingId;
        const choices = groups.get(key) || [];
        choices.push(input);
        groups.set(key, choices);
    });
    groups.forEach(function (choices) {
        const hasSubtotal = choices.some(function (input) { return input.dataset.fsChoiceKind === 'subtotal' && input.checked; });
        const hasCategory = choices.some(function (input) { return input.dataset.fsChoiceKind === 'category' && input.checked; });
        choices.forEach(function (input) {
            const row = input.closest('[data-fs-choice-row]');
            const operator = row.querySelector('[data-fs-calc-operator]');
            const directConflict = (input.dataset.fsChoiceKind === 'subtotal' && hasCategory) || (input.dataset.fsChoiceKind === 'category' && hasSubtotal);
            const lineage = inputLineage(input);
            const overlap = selected.find(function (item) {
                if (item.input === input) return false;
                return Array.from(lineage).some(function (source) { return item.lineage.has(source); });
            });
            const disabled = !input.checked && (directConflict || !!overlap);
            input.disabled = disabled;
            operator.disabled = !input.checked;
            row.classList.toggle('is-unavailable', disabled);
            const note = row.querySelector('[data-fs-choice-overlap]');
            note.textContent = overlap ? ((overlap.kind === 'total' || overlap.kind === 'subtotal') ? 'Included via ' : 'Overlaps ') + overlap.label : '';
            note.hidden = !overlap;
            if (overlap) row.title = note.textContent;
            else row.removeAttribute('title');
        });
    });
}
function accFinancialSortRows(group) {
    return Array.from(group.children).filter(function (node) { return node.matches('[data-fs-sort-row]'); });
}
function accClearFinancialDropState(panel) {
    panel.querySelectorAll('.acc2-fs-drop-before, .acc2-fs-drop-after, .acc2-fs-dragging').forEach(function (node) {
        node.classList.remove('acc2-fs-drop-before', 'acc2-fs-drop-after', 'acc2-fs-dragging');
    });
}
function accHandleFinancialDragStart(event) {
    const handle = event.target.closest('[data-fs-drag-handle]');
    const row = handle && handle.closest('[data-fs-sort-row]');
    const panel = row && row.closest('[data-fs-section]');
    if (!row || !event.dataTransfer || panel.getAttribute('aria-busy') === 'true') { event.preventDefault(); return; }
    accFinancialDraggingId = row.dataset.fsId;
    event.dataTransfer.effectAllowed = 'move';
    event.dataTransfer.setData('text/plain', accFinancialDraggingId);
    row.classList.add('acc2-fs-dragging');
}
function accHandleFinancialDragOver(event) {
    const target = event.target.closest('[data-fs-sort-row]');
    const source = accFinancialDraggingId && document.querySelector('[data-fs-sort-row][data-fs-id="' + CSS.escape(accFinancialDraggingId) + '"]');
    if (!target || !source || target === source || target.dataset.fsSortGroup !== source.dataset.fsSortGroup) return;
    event.preventDefault();
    if (event.dataTransfer) event.dataTransfer.dropEffect = 'move';
    const after = event.clientY > target.getBoundingClientRect().top + target.getBoundingClientRect().height / 2;
    target.classList.toggle('acc2-fs-drop-after', after);
    target.classList.toggle('acc2-fs-drop-before', !after);
}
async function accHandleFinancialDrop(event) {
    const target = event.target.closest('[data-fs-sort-row]');
    const source = accFinancialDraggingId && document.querySelector('[data-fs-sort-row][data-fs-id="' + CSS.escape(accFinancialDraggingId) + '"]');
    if (!target || !source || target === source || target.dataset.fsSortGroup !== source.dataset.fsSortGroup) return;
    event.preventDefault();
    const group = source.parentElement;
    const rows = accFinancialSortRows(group);
    const ids = rows.map(function (row) { return row.dataset.fsId; });
    const sourceIndex = ids.indexOf(source.dataset.fsId);
    const targetIndex = ids.indexOf(target.dataset.fsId);
    if (sourceIndex < 0 || targetIndex < 0) return;
    ids.splice(sourceIndex, 1);
    const after = event.clientY > target.getBoundingClientRect().top + target.getBoundingClientRect().height / 2;
    const adjustedTargetIndex = targetIndex - (sourceIndex < targetIndex ? 1 : 0);
    const insertAt = adjustedTargetIndex + (after ? 1 : 0);
    ids.splice(Math.min(insertAt, ids.length), 0, source.dataset.fsId);
    if (ids.every(function (id, index) { return id === rows[index].dataset.fsId; })) return;
    const panel = source.closest('[data-fs-section]');
    const orderStatus = panel && panel.querySelector('[data-fs-order-status]');
    if (panel) panel.setAttribute('aria-busy', 'true');
    if (orderStatus && window.LoadingUI) window.LoadingUI.showSpinner(orderStatus, 'Saving order', { escalate: false });
    try {
        await accApi('/api/account-control-center/categories/order', { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ client_id: accClientId, category_ids: ids.map(Number) }) });
        await accLoadFinancialCategories();
        accShowStatus('Financial Statement order updated.', 'success');
    } finally {
        if (orderStatus && window.LoadingUI) window.LoadingUI.hideSpinner(orderStatus);
        if (panel) panel.removeAttribute('aria-busy');
    }
}
function accFinancialKey(label) { return String(label || '').normalize('NFKD').toLowerCase().replace(/[^a-z0-9]+/g, '_').replace(/^_+|_+$/g, '').slice(0, 70) || 'category'; }
function accOpenFinancialCalculationEditor(section, calculationType, headingId, calculationId) {
    const calculation = calculationId ? accFinancialCalculations.find(function (item) { return Number(item.id) === Number(calculationId); }) : null;
    const heading = headingId ? accFinancialCategories.find(function (item) { return Number(item.id) === Number(headingId); }) : null;
    const components = calculation ? calculation.components || [] : calculationType === 'subtotal'
        ? accFinancialCategories.filter(function (item) { return item.statement_section === section && item.row_type === 'category' && Number(item.parent_category_id) === Number(headingId); }).map(function (item) { return { source_category_id: item.id, operator: 'add' }; })
        : [];
    accFinancialCalculationEditor = {
        statement_section: calculation ? calculation.statement_section : section,
        calculation_id: calculation ? calculation.id : null,
        calculation_type: calculation ? calculation.calculation_type : calculationType,
        parent_heading_id: calculation ? calculation.parent_heading_id : (heading ? heading.id : null),
        label: calculation ? calculation.label : (calculationType === 'subtotal' ? 'Total ' + heading.label : ''),
        sort_order: calculation ? calculation.sort_order : accFinancialCalculations.filter(function (item) { return item.statement_section === section && item.calculation_type === 'total'; }).reduce(function (last, item) { return Math.max(last, Number(item.sort_order) + 1); }, 0),
        components: components.map(function (item) { return { ...item }; }),
    };
    accRenderFinancialOutlines();
    const labelInput = document.querySelector('[data-fs-section="' + accFinancialCalculationEditor.statement_section + '"] [data-fs-calc-label]');
    if (labelInput) { labelInput.focus(); labelInput.select(); }
}
async function accHandleFinancialCalculationAction(event) {
    const button = event.target.closest('[data-fs-calc-action]');
    if (!button) return;
    const action = button.dataset.fsCalcAction;
    const section = button.closest('[data-fs-section]')?.dataset.fsSection || accFinancialCalculationEditor?.statement_section || 'balance_sheet';
    if (action === 'cancel') {
        accFinancialCalculationEditor = null;
        accRenderFinancialOutlines();
        return;
    }
    if (action === 'add-subtotal') {
        accOpenFinancialCalculationEditor(section, 'subtotal', Number(button.dataset.fsHeadingId));
        return;
    }
    if (action === 'add-total') {
        accOpenFinancialCalculationEditor(section, 'total', null);
        return;
    }
    if (action === 'edit') {
        accOpenFinancialCalculationEditor(section, null, null, Number(button.dataset.fsCalcId));
        return;
    }
    if (action === 'delete') {
        const calculation = accFinancialCalculations.find(function (item) { return Number(item.id) === Number(button.dataset.fsCalcId); });
        if (!calculation || !window.confirm('Delete calculation line “' + calculation.label + '”?')) return;
        await accApi('/api/account-control-center/statement-calculations/' + calculation.id + '?client_id=' + encodeURIComponent(accClientId), { method: 'DELETE' });
        await accLoadFinancialCategories();
        accShowStatus('Deleted calculation line “' + calculation.label + '”.', 'success');
        return;
    }
    if (action !== 'save') return;
    const editor = button.closest('[data-fs-calc-editor]');
    const label = editor.querySelector('[data-fs-calc-label]').value.trim();
    const components = Array.from(editor.querySelectorAll('[data-fs-calc-include]')).filter(function (input) {
        return input.type === 'hidden' || input.checked;
    }).map(function (input) {
        const row = input.closest('.acc2-fs-calc-choice');
        const component = { operator: row.querySelector('[data-fs-calc-operator]').value };
        if (input.dataset.sourceCategoryId) component.source_category_id = Number(input.dataset.sourceCategoryId);
        if (input.dataset.sourceCalculationId) component.source_calculation_id = Number(input.dataset.sourceCalculationId);
        return component;
    });
    if (!label) { accShowStatus('Enter a label for this calculation line.', 'error'); return; }
    if (!components.length) { accShowStatus('Select at least one row for this total.', 'error'); return; }
    const state = accFinancialCalculationEditor;
    const payload = {
        client_id: accClientId,
        statement_section: state.statement_section,
        calculation_type: state.calculation_type,
        label: label,
        parent_heading_id: state.calculation_type === 'subtotal' ? Number(state.parent_heading_id) : null,
        sort_order: state.sort_order,
        components: components,
    };
    await accWithButtonBusy(button, 'Saving line', async function () {
        const method = state.calculation_id ? 'PUT' : 'POST';
        const url = '/api/account-control-center/statement-calculations' + (state.calculation_id ? '/' + state.calculation_id : '');
        await accApi(url, { method: method, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) });
    });
    accFinancialCalculationEditor = null;
    await accLoadFinancialCategories();
    accShowStatus('Saved calculation line “' + label + '”.', 'success');
}
function accHandleCalculationIncludeChange(event) {
    const input = event.target.closest('[data-fs-calc-include]');
    if (!input || input.type !== 'checkbox') return;
    const editor = input.closest('[data-fs-calc-editor]');
    accUpdateFinancialTotalChoiceAvailability(editor);
    accUpdateFinancialCalculationPlacementPreview(editor);
}
async function accHandleFinancialOutlineAction(event) {
    const button = event.target.closest('[data-fs-action]');
    if (!button) return;
    const action = button.dataset.fsAction;
    const panel = button.closest('[data-fs-section]');
    const section = panel.dataset.fsSection;
    const item = accFinancialCategories.find(function (row) { return Number(row.id) === Number(button.dataset.fsId); });
    if (action === 'add-heading' || action === 'add-category') {
        const label = window.prompt(action === 'add-heading' ? 'Heading name' : 'Category name');
        if (!label || !label.trim()) return;
        const parentId = button.dataset.fsParent ? Number(button.dataset.fsParent) : null;
        const body = { client_id: accClientId, category_type: 'financial_statement', category_key: accFinancialKey(label), label: label.trim(), statement_section: section, row_type: action === 'add-heading' ? 'heading' : 'category', parent_category_id: parentId, sort_order: accFinancialCategories.length };
        await accApi('/api/account-control-center/categories', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
    } else if (item && action === 'rename') {
        const label = window.prompt('Rename category', item.label);
        if (!label || !label.trim()) return;
        await accApi('/api/account-control-center/categories/' + item.id, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ client_id: accClientId, category_type: 'financial_statement', label: label.trim(), statement_section: item.statement_section, row_type: item.row_type, parent_category_id: item.parent_category_id, sort_order: item.sort_order, is_active: item.is_active }) });
    } else if (item && action === 'toggle') {
        await accApi('/api/account-control-center/categories/' + item.id, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ client_id: accClientId, category_type: 'financial_statement', label: item.label, statement_section: item.statement_section, row_type: item.row_type, parent_category_id: item.parent_category_id, sort_order: item.sort_order, is_active: !item.is_active }) });
    } else if (item && action === 'delete') {
        const children = item.row_type === 'heading' ? accFinancialCategories.filter(function (row) { return Number(row.parent_category_id) === Number(item.id); }) : [];
        const prompt = children.length
            ? 'Delete heading “' + item.label + '” and all ' + children.length + ' child categor' + (children.length === 1 ? 'y' : 'ies') + '? This cannot be undone. The heading and its children must have no Trial Balance account assignments.'
            : 'Permanently delete category “' + item.label + '”? It must have no Trial Balance account assignments. This cannot be undone.';
        if (!window.confirm(prompt)) return;
        const result = await accApi('/api/account-control-center/categories/' + item.id + '?client_id=' + encodeURIComponent(accClientId), { method: 'DELETE' });
        await accLoadFinancialCategories();
        const childCount = Number(result.deleted_child_count || 0);
        accShowStatus(childCount ? 'Deleted heading and ' + childCount + ' child categor' + (childCount === 1 ? 'y.' : 'ies.') : 'Deleted category “' + item.label + '”.', 'success');
        return;
    } else return;
    await accLoadFinancialCategories();
}
function accRenderNotesOutlines() {
    document.querySelectorAll('[data-notes-section]').forEach(function (panel) {
        const section = panel.dataset.notesSection;
        const headings = accFinancialCategories.filter(function (row) { return row.statement_section === section && row.row_type === 'heading'; });
        const rows = accFinancialCategories.filter(function (row) { return row.statement_section === section && row.row_type !== 'heading'; });
        const roots = accNoteCategories.filter(function (row) { return row.source_statement_category_id != null; });
        const legacy = accNoteCategories.filter(function (row) { return row.source_statement_category_id == null && row.parent_category_id == null; });
        function breakdownMarkup(item) {
            const children = accNoteCategories.filter(function (row) { return Number(row.parent_category_id) === Number(item.id); });
            return '<div class="acc2-note-breakdown" data-note-row data-note-id="' + item.id + '"><div class="acc2-note-row"><button type="button" class="acc2-fs-drag-handle" draggable="true" data-note-drag-handle aria-label="Move breakdown ' + accEscape(item.label) + '" title="Drag to reorder or move">☰</button><span>' + accEscape(item.label) + '</span><button type="button" class="acc2-fs-small" data-note-action="add" data-note-parent="' + item.id + '">+ Breakdown</button><button type="button" class="acc2-fs-small" data-note-action="rename" data-note-id="' + item.id + '">Rename</button><button type="button" class="acc2-fs-small" data-note-action="delete" data-note-id="' + item.id + '">Delete</button></div><div class="acc2-note-children" data-note-drop-parent="' + item.id + '">' + children.map(breakdownMarkup).join('') + '</div></div>';
        }
        function sourceRow(source) {
            const root = roots.find(function (item) { return Number(item.source_statement_category_id) === Number(source.id); });
            const drag = root ? '<button type="button" class="acc2-fs-drag-handle" draggable="true" data-note-anchor-drag-handle data-note-anchor-id="' + root.id + '" aria-label="Move note heading ' + accEscape(source.label) + '" title="Drag this note to another category in this statement">☰</button>' : '';
            return '<div class="acc2-notes-source-row" data-note-source-drop data-source-id="' + source.id + '"' + (root ? ' data-note-anchor-row data-note-anchor-id="' + root.id + '"' : '') + '>' + drag + '<span class="acc2-notes-source-label">' + accEscape(source.label) + '</span>' + (root ? '<button type="button" class="acc2-fs-small" data-note-action="add-child" data-note-parent="' + root.id + '">+ Breakdown</button><button type="button" class="acc2-fs-small" data-note-action="delete" data-note-id="' + root.id + '">Remove note</button><div class="acc2-note-children" data-note-drop-parent="' + root.id + '">' + accNoteCategories.filter(function (item) { return Number(item.parent_category_id) === Number(root.id); }).map(breakdownMarkup).join('') + '</div>' : '<button type="button" class="acc2-fs-small primary" data-note-action="link" data-source-id="' + source.id + '">+ Add note</button>') + '</div>';
        }
        const groups = headings.map(function (heading) {
            const children = rows.filter(function (row) { return Number(row.parent_category_id) === Number(heading.id); });
            return '<section class="acc2-notes-group"><h3>' + accEscape(heading.label) + '</h3>' + (children.map(sourceRow).join('') || '<p class="acc2-fs-empty">No statement categories in this heading.</p>') + '</section>';
        }).join('');
        const standalone = rows.filter(function (row) { return !row.parent_category_id; }).map(sourceRow).join('');
        const legacyMarkup = legacy.map(breakdownMarkup).join('');
        panel.innerHTML = '<div class="acc2-fs-toolbar"><span>Notes follow their statement row. Add breakdowns as needed; drag ☰ to reorder or move them.</span><span class="acc2-fs-order-status" data-note-order-status role="status" aria-live="polite"></span></div>' + groups + (standalone ? '<section class="acc2-notes-group"><h3>Standalone statement rows</h3>' + standalone + '</section>' : '') + (legacyMarkup ? '<section class="acc2-notes-group legacy"><h3>Unlinked / legacy notes</h3>' + legacyMarkup + '</section>' : '') + (!groups && !standalone && !legacyMarkup ? '<div class="acc2-fs-empty-state">Add a Financial Statement category first.</div>' : '');
    });
}
async function accLoadNoteCategories() {
    const result = await accApi('/api/account-control-center/categories?client_id=' + encodeURIComponent(accClientId) + '&category_type=note&include_inactive=true');
    accNoteCategories = result.categories || [];
    accNotesLoaded = true;
    accRenderNotesRows();
    accRenderNotesOutlines();
}
async function accLoadNotesSetupData() {
    const loads = [];
    if (!accFinancialLoaded) loads.push(accLoadFinancialCategories());
    if (!accNotesLoaded) loads.push(accLoadNoteCategories());
    await Promise.all(loads);
    accRenderNotesRows();
    accRenderNotesOutlines();
}
function accMappingPayload(accountCode, workstream) {
    return Object.assign({ client_id: accClientId, account_code: accountCode, verification_workstream: workstream || null }, accSelectedUploadPayload());
}
async function accSaveLeaf(account, select) {
    const result = accElement('accAccountRows').querySelector('[data-acc-result="' + CSS.escape(account.account_code) + '"]');
    accSetInlineBusy(select, result, 'Saving...');
    let saved = false;
    try {
        await accWithBusy('Saving Verification assignment...', function () {
            return accApi('/api/account-control-center/mapping', { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(accMappingPayload(account.account_code, select.value)) });
        });
        accAssignment(account).verification_workstream = select.value || null;
        saved = true;
        accShowStatus('Saved ' + account.account_code + '.', 'success');
    } finally {
        accSetInlineResult(select, result, saved);
    }
}
async function accApplyParent(account, select) {
    if (!window.confirm('Apply this verification sub-module to all leaf accounts below ' + account.account_code + '?')) { accRenderRows(); return; }
    accSetSelectBusy(select, true);
    try {
        await accWithTableBusy('accGridWrap', 'Applying Verification assignment to child accounts...', async function () {
            await accApi('/api/account-control-center/mapping/apply-to-children', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(Object.assign({ client_id: accClientId, parent_account_code: account.account_code, verification_workstream: select.value || null }, accSelectedUploadPayload())) });
            await accLoadMapping(true);
        });
        accShowStatus('Applied the assignment to the child accounts below ' + account.account_code + '.', 'success');
    } finally {
        accSetSelectBusy(select, false);
    }
}
async function accHandleWorkstreamChange(event) {
    const select = event.target.closest('.acc2-workstream');
    if (!select) return;
    const account = accAccounts.find(function (item) { return accNormaliseCode(item.account_code) === accNormaliseCode(select.dataset.accCode); });
    if (!account) return;
    accClearStatus();
    try {
        if (select.dataset.accParent === 'true') await accApplyParent(account, select);
        else await accSaveLeaf(account, select);
    } catch (error) {
        accShowStatus(error.message, 'error');
        await accLoadMapping(true).catch(function () {});
    }
}
async function accSaveCashflowCategory(account, select) {
    const categoryId = select.value && select.value !== '__mixed' ? Number(select.value) : null;
    const result = accElement('accCashflowRows').querySelector('[data-acc-cashflow-result="' + CSS.escape(account.account_code) + '"]');
    accSetInlineBusy(select, result, 'Saving...');
    let saved = false;
    try {
        await accWithBusy('Saving Cash Flow category...', function () {
            return accApi('/api/account-control-center/cashflow/category-assignment', { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(Object.assign({ client_id: accClientId, account_code: account.account_code, category_id: categoryId }, accSelectedUploadPayload())) });
        });
        account.category_id = categoryId;
        saved = true;
        accShowStatus('Saved Cash Flow category for ' + account.account_code + '.', 'success');
    } finally {
        accSetInlineResult(select, result, saved);
    }
}
async function accApplyCashflowCategoryParent(account, select) {
    if (!window.confirm('Apply this Cash Flow category to all leaf accounts below ' + account.account_code + '?')) { accRenderCashflowRows(); return; }
    const categoryId = select.value && select.value !== '__mixed' ? Number(select.value) : null;
    accSetSelectBusy(select, true);
    try {
        await accWithTableBusy('accCashflowGridWrap', 'Applying Cash Flow category to child accounts...', async function () {
            await accApi('/api/account-control-center/cashflow/category-assignment/apply-to-children', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(Object.assign({ client_id: accClientId, parent_account_code: account.account_code, category_id: categoryId }, accSelectedUploadPayload())) });
            await accLoadCashflow();
        });
        accShowStatus('Applied the Cash Flow category to child accounts below ' + account.account_code + '.', 'success');
    } finally {
        accSetSelectBusy(select, false);
    }
}
async function accHandleCashflowChange(event) {
    const select = event.target.closest('.acc2-cashflow-category');
    if (!select) return;
    const account = accAccounts.find(function (item) { return accNormaliseCode(item.account_code) === accNormaliseCode(select.dataset.accCode); });
    const orphan = accCashflowHistoricalOrphans.find(function (item) { return accNormaliseCode(item.account_code) === accNormaliseCode(select.dataset.accCode); });
    const target = account || orphan;
    if (!target) return;
    accClearStatus();
    try {
        if (select.dataset.accParent === 'true') await accApplyCashflowCategoryParent(target, select);
        else await accSaveCashflowCategory(target, select);
    } catch (error) {
        accShowStatus(error.message, 'error');
        await accLoadCashflow().catch(function () {});
    }
}
async function accSaveFinancialLeaf(account, select) {
    const result = accElement('accFinancialRows').querySelector('[data-acc-fs-result="' + CSS.escape(account.account_code) + '"]');
    const value = select.value ? Number(select.value) : null;
    accSetInlineBusy(select, result, 'Saving...');
    let saved = false;
    try {
        await accApi('/api/account-control-center/mapping', { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(Object.assign({ client_id: accClientId, account_code: account.account_code, financial_statement_category_id: value }, accSelectedUploadPayload())) });
        accAssignment(account).financial_statement_category_id = value;
        saved = true;
        if (accNotesLoaded) accRenderNotesRows();
        accShowStatus('Saved Financial Statement category for ' + account.account_code + '.', 'success');
    } finally {
        accSetInlineResult(select, result, saved);
    }
}
async function accSaveNoteLeaf(account, select) {
    const result = accElement('accNotesRows').querySelector('[data-acc-note-result="' + CSS.escape(account.account_code) + '"]');
    const value = select.value ? Number(select.value) : null;
    accSetInlineBusy(select, result, 'Saving...');
    let saved = false;
    try {
        await accWithBusy('Saving note assignment...', function () {
            return accApi('/api/account-control-center/mapping', { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(Object.assign({ client_id: accClientId, account_code: account.account_code, note_category_id: value }, accSelectedUploadPayload())) });
        });
        accAssignment(account).note_category_id = value;
        saved = true;
        accShowStatus('Saved note for ' + account.account_code + '.', 'success');
    } finally {
        accSetInlineResult(select, result, saved);
    }
}
async function accApplyNoteParent(account, select) {
    if (!window.confirm('Apply this note to all leaf accounts below ' + account.account_code + '?')) { accRenderNotesRows(); return; }
    const value = select.value ? Number(select.value) : null;
    accSetSelectBusy(select, true);
    try {
        await accWithTableBusy('accNotesGridWrap', 'Applying note to child accounts...', async function () {
            await accApi('/api/account-control-center/mapping/apply-to-children', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(Object.assign({ client_id: accClientId, parent_account_code: account.account_code, note_category_id: value }, accSelectedUploadPayload())) });
            await accLoadMapping(true);
        });
        accShowStatus('Applied the note to child accounts below ' + account.account_code + '.', 'success');
    } finally {
        accSetSelectBusy(select, false);
    }
}
async function accApplyFinancialParent(account, select) {
    if (!window.confirm('Apply this Financial Statement category to all leaf accounts below ' + account.account_code + '?')) { accRenderFinancialRows(); return; }
    const value = select.value ? Number(select.value) : null;
    accSetSelectBusy(select, true);
    try {
        await accWithTableBusy('accFinancialGridWrap', 'Applying Financial Statement category to child accounts...', async function () {
            await accApi('/api/account-control-center/mapping/apply-to-children', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(Object.assign({ client_id: accClientId, parent_account_code: account.account_code, financial_statement_category_id: value }, accSelectedUploadPayload())) });
            await accLoadMapping(true);
        });
        accShowStatus('Applied the Financial Statement category to child accounts below ' + account.account_code + '.', 'success');
    } finally {
        accSetSelectBusy(select, false);
    }
}
function accFilterFinancialPicker(picker) {
    const searchInput = picker.querySelector('[data-fs-picker-search-input]');
    const term = String(searchInput && searchInput.value || '').trim().toLowerCase();
    const options = Array.from(picker.querySelectorAll('[data-fs-picker-option]'));
    options.forEach(function (option) { option.hidden = !!term && String(option.dataset.fsPickerSearch || '').indexOf(term) === -1; });
    picker.querySelectorAll('[data-fs-picker-heading-group]').forEach(function (group) {
        group.hidden = !group.querySelector('[data-fs-picker-option]:not([hidden])');
    });
    picker.querySelectorAll('[data-fs-picker-section]').forEach(function (section) {
        section.hidden = !!term && !section.querySelector('[data-fs-picker-option]:not([hidden])');
    });
    const visibleCount = options.filter(function (option) { return !option.hidden && !option.disabled; }).length;
    const noResults = picker.querySelector('[data-fs-picker-no-results]');
    if (noResults) noResults.hidden = visibleCount > 0;
}
function accPositionFinancialPicker(picker) {
    const trigger = picker.querySelector('[data-fs-picker-trigger]');
    const menu = picker.querySelector('[data-fs-picker-menu]');
    if (!trigger || !menu) return;
    const rect = trigger.getBoundingClientRect();
    const maxHeight = Math.min(380, Math.max(180, window.innerHeight - 24));
    const width = Math.min(Math.max(rect.width, 340), window.innerWidth - 24);
    menu.style.width = width + 'px';
    menu.style.maxHeight = maxHeight + 'px';
    menu.style.visibility = 'hidden';
    menu.hidden = false;
    const height = Math.min(menu.scrollHeight, maxHeight);
    const left = Math.max(12, Math.min(rect.left, window.innerWidth - width - 12));
    let top = rect.bottom + 5;
    if (top + height > window.innerHeight - 10 && rect.top > height + 10) top = rect.top - height - 5;
    menu.style.left = left + 'px';
    menu.style.top = Math.max(10, Math.min(top, window.innerHeight - height - 10)) + 'px';
    menu.style.visibility = '';
}
function accCloseFinancialPicker(picker, returnFocus) {
    if (!picker) return;
    const trigger = picker.querySelector('[data-fs-picker-trigger]');
    const menu = picker.querySelector('[data-fs-picker-menu]');
    if (menu) menu.hidden = true;
    if (trigger) {
        trigger.setAttribute('aria-expanded', 'false');
        if (returnFocus) trigger.focus();
    }
    picker.classList.remove('is-open');
}
function accCloseFinancialPickers(except) {
    document.querySelectorAll('[data-fs-picker].is-open').forEach(function (picker) { if (picker !== except) accCloseFinancialPicker(picker, false); });
}
function accOpenFinancialPicker(picker) {
    const trigger = picker && picker.querySelector('[data-fs-picker-trigger]');
    const menu = picker && picker.querySelector('[data-fs-picker-menu]');
    if (!trigger || !menu || trigger.disabled) return;
    accCloseFinancialPickers(picker);
    picker.classList.add('is-open');
    trigger.setAttribute('aria-expanded', 'true');
    const searchInput = picker.querySelector('[data-fs-picker-search-input]');
    if (searchInput) searchInput.value = '';
    accFilterFinancialPicker(picker);
    accPositionFinancialPicker(picker);
    if (searchInput) searchInput.focus();
}
function accUpdateFinancialPickerSelection(select) {
    const picker = select && select.closest('[data-fs-picker]');
    if (!picker) return;
    const label = picker.querySelector('[data-fs-picker-value-label]');
    if (label) {
        if (select.matches('.acc2-note-category')) {
            const account = accAccounts.find(function (item) { return accNormaliseCode(item.account_code) === accNormaliseCode(select.dataset.accCode); });
            const statementCategoryId = account ? accEffectiveFinancialCategory(account) : '';
            const options = statementCategoryId && statementCategoryId !== '__mixed' ? accGetEffectiveNoteOptions(statementCategoryId) : [];
            label.textContent = accNotePickerLabel(statementCategoryId, select.value, options);
        } else {
            label.textContent = accFinancialPickerLabel(select.value);
        }
    }
    picker.querySelectorAll('[data-fs-picker-option]').forEach(function (option) {
        option.setAttribute('aria-selected', String(option.dataset.fsPickerValue) === String(select.value) ? 'true' : 'false');
    });
}
function accHandleFinancialPickerClick(event) {
    const trigger = event.target.closest('[data-fs-picker-trigger]');
    if (trigger) {
        const picker = trigger.closest('[data-fs-picker]');
        if (picker.classList.contains('is-open')) accCloseFinancialPicker(picker, false);
        else accOpenFinancialPicker(picker);
        return;
    }
    const option = event.target.closest('[data-fs-picker-option]');
    if (!option || option.disabled || option.hidden) return;
    const picker = option.closest('[data-fs-picker]');
    const select = picker && picker.querySelector('.acc2-financial-category, .acc2-note-category');
    if (!select) return;
    const value = option.dataset.fsPickerValue || '';
    accCloseFinancialPicker(picker, true);
    if (select.value === value) return;
    select.value = value;
    accUpdateFinancialPickerSelection(select);
    select.dispatchEvent(new Event('change', { bubbles: true }));
}
function accHandleFinancialPickerInput(event) {
    const searchInput = event.target.closest('[data-fs-picker-search-input]');
    if (searchInput) accFilterFinancialPicker(searchInput.closest('[data-fs-picker]'));
}
function accHandleFinancialPickerKeydown(event) {
    const trigger = event.target.closest('[data-fs-picker-trigger]');
    if (trigger && (event.key === 'ArrowDown' || event.key === 'ArrowUp')) {
        event.preventDefault();
        accOpenFinancialPicker(trigger.closest('[data-fs-picker]'));
        const options = trigger.closest('[data-fs-picker]').querySelectorAll('[data-fs-picker-option]:not([hidden]):not(:disabled)');
        if (options.length) options[event.key === 'ArrowUp' ? options.length - 1 : 0].focus();
        return;
    }
    const picker = event.target.closest('[data-fs-picker]');
    if (!picker || !picker.classList.contains('is-open')) return;
    if (event.key === 'Escape') {
        event.preventDefault();
        accCloseFinancialPicker(picker, true);
        return;
    }
    const option = event.target.closest('[data-fs-picker-option]');
    if (!option) {
        if (event.target.matches('[data-fs-picker-search-input]') && event.key === 'ArrowDown') {
            const first = picker.querySelector('[data-fs-picker-option]:not([hidden]):not(:disabled)');
            if (first) { event.preventDefault(); first.focus(); }
        }
        return;
    }
    if (!['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(event.key)) return;
    const options = Array.from(picker.querySelectorAll('[data-fs-picker-option]:not([hidden]):not(:disabled)'));
    const index = options.indexOf(option);
    const nextIndex = event.key === 'Home' ? 0 : (event.key === 'End' ? options.length - 1 : (index + (event.key === 'ArrowDown' ? 1 : -1) + options.length) % options.length);
    if (options[nextIndex]) { event.preventDefault(); options[nextIndex].focus(); }
}
async function accHandleFinancialChange(event) {
    const select = event.target.closest('.acc2-financial-category');
    if (!select) return;
    accUpdateFinancialPickerSelection(select);
    const account = accAccounts.find(function (item) { return accNormaliseCode(item.account_code) === accNormaliseCode(select.dataset.accCode); });
    if (!account) return;
    accClearStatus();
    try {
        if (select.dataset.accParent === 'true') await accApplyFinancialParent(account, select);
        else await accSaveFinancialLeaf(account, select);
    } catch (error) {
        accShowStatus(error.message, 'error');
        await accLoadMapping(true).catch(function () {});
    }
}
async function accHandleNoteChange(event) {
    const select = event.target.closest('.acc2-note-category');
    if (!select) return;
    const account = accAccounts.find(function (item) { return accNormaliseCode(item.account_code) === accNormaliseCode(select.dataset.accCode); });
    if (!account) return;
    accClearStatus();
    try {
        if (select.dataset.accParent === 'true') await accApplyNoteParent(account, select);
        else await accSaveNoteLeaf(account, select);
    } catch (error) {
        accShowStatus(error.message, 'error');
        await accLoadMapping(true).catch(function () {});
    }
}
async function accUpdateNoteCategory(category, patch, message) {
    await accApi('/api/account-control-center/categories/' + category.id, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ client_id: accClientId, category_type: 'note', label: patch.label == null ? category.label : patch.label, parent_category_id: category.parent_category_id, source_statement_category_id: category.source_statement_category_id, sort_order: patch.sort_order == null ? category.sort_order : patch.sort_order, is_active: patch.is_active == null ? category.is_active : patch.is_active }) });
    await accLoadNoteCategories();
    accShowStatus(message, 'success');
}
async function accHandleNotesOutlineAction(event) {
    const button = event.target.closest('[data-note-action]');
    if (!button) return;
    const action = button.dataset.noteAction;
    if (action === 'link') {
        const source = accFinancialCategories.find(function (item) { return Number(item.id) === Number(button.dataset.sourceId); });
        if (!source) return;
        await accApi('/api/account-control-center/categories', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ client_id: accClientId, category_type: 'note', category_key: 'statement_' + source.id, label: source.label, source_statement_category_id: source.id, sort_order: source.sort_order }) });
        await accLoadNoteCategories();
        accShowStatus('Note heading linked to ' + source.label + '.', 'success');
        return;
    }
    const item = accNoteCategories.find(function (row) { return Number(row.id) === Number(button.dataset.noteId || button.dataset.noteParent); });
    if (action === 'add' || action === 'add-child') {
        const parentId = Number(button.dataset.noteParent);
        const label = window.prompt('Breakdown name');
        if (!label || !label.trim()) return;
        await accApi('/api/account-control-center/categories', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ client_id: accClientId, category_type: 'note', category_key: 'note_' + Date.now(), label: label.trim(), parent_category_id: parentId, sort_order: accNoteCategories.length }) });
        await accLoadNoteCategories();
        accShowStatus('Note breakdown added.', 'success');
    } else if (item && action === 'rename') {
        const label = window.prompt('Rename breakdown', item.label);
        if (!label || !label.trim() || label.trim() === item.label) return;
        await accUpdateNoteCategory(item, { label: label.trim() }, 'Note breakdown renamed.');
    } else if (item && action === 'delete') {
        const descendants = (function count(id) { const children = accNoteCategories.filter(function (row) { return Number(row.parent_category_id) === Number(id); }); return children.reduce(function (total, child) { return total + 1 + count(child.id); }, 0); })(item.id);
        if (!window.confirm('Delete “' + item.label + '”' + (descendants ? ' and its ' + descendants + ' child breakdown(s)' : '') + '? Assigned note categories cannot be deleted.')) return;
        const result = await accApi('/api/account-control-center/categories/' + item.id + '?client_id=' + encodeURIComponent(accClientId), { method: 'DELETE' });
        await accLoadNoteCategories();
        accShowStatus(result.deleted_child_count ? 'Note breakdown and child rows deleted.' : 'Note breakdown deleted.', 'success');
    }
}
function accNoteSubtreeAssignedCount(categoryId) {
    const category = accNoteCategories.find(function (item) { return Number(item.id) === Number(categoryId); });
    const own = Number(category && category.assigned_account_count || 0);
    const children = accNoteCategories.filter(function (item) { return Number(item.parent_category_id) === Number(categoryId); });
    return own + children.reduce(function (total, child) { return total + accNoteSubtreeAssignedCount(child.id); }, 0);
}
function accHandleNotesDragStart(event) {
    const anchorHandle = event.target.closest('[data-note-anchor-drag-handle]');
    if (anchorHandle) {
        const anchorRow = anchorHandle.closest('[data-note-anchor-row]');
        if (!anchorRow || !event.dataTransfer) { event.preventDefault(); return; }
        accNoteDraggingAnchorId = anchorRow.dataset.noteAnchorId;
        accNoteDraggingId = null;
        event.dataTransfer.effectAllowed = 'move';
        event.dataTransfer.setData('text/plain', accNoteDraggingAnchorId);
        anchorRow.classList.add('acc2-note-dragging');
        return;
    }
    const handle = event.target.closest('[data-note-drag-handle]');
    const row = handle && handle.closest('[data-note-row]');
    if (!row || !event.dataTransfer) { event.preventDefault(); return; }
    accNoteDraggingId = row.dataset.noteId;
    accNoteDraggingAnchorId = null;
    event.dataTransfer.effectAllowed = 'move';
    event.dataTransfer.setData('text/plain', accNoteDraggingId);
    row.classList.add('acc2-note-dragging');
}
async function accHandleNotesDrop(event) {
    const panel = event.currentTarget;
    if (accNoteDraggingAnchorId) {
        const targetSource = event.target.closest('[data-note-source-drop]');
        const note = accNoteCategories.find(function (item) { return Number(item.id) === Number(accNoteDraggingAnchorId); });
        const source = targetSource && accFinancialCategories.find(function (item) { return Number(item.id) === Number(targetSource.dataset.sourceId); });
        if (!note || !targetSource || !source || Number(note.source_statement_category_id) === Number(source.id)) return;
        event.preventDefault();
        if (source.statement_section !== panel.dataset.notesSection) return;
        const assigned = accNoteSubtreeAssignedCount(note.id);
        const warning = assigned
            ? 'This note has ' + assigned + ' currently assigned Trial Balance account' + (assigned === 1 ? '' : 's') + '. Move it to “' + source.label + '”? The note and its assignments will stay together, and future statements will report them under the new category.'
            : 'Move this note to “' + source.label + '”? Its breakdowns will move with it.';
        if (!window.confirm(warning)) return;
        const status = panel.querySelector('[data-note-order-status]');
        panel.setAttribute('aria-busy', 'true');
        if (status && window.LoadingUI) window.LoadingUI.showSpinner(status, 'Moving note', { escalate: false });
        try {
            await accApi('/api/account-control-center/categories/' + note.id, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ client_id: accClientId, category_type: 'note', label: note.label, statement_section: null, row_type: 'category', parent_category_id: null, source_statement_category_id: source.id, sort_order: source.sort_order, is_active: note.is_active }) });
            await accLoadNoteCategories();
            accShowStatus('Note moved. Its account assignments remain linked.', 'success');
        } finally {
            if (status && window.LoadingUI) window.LoadingUI.hideSpinner(status);
            panel.removeAttribute('aria-busy');
        }
        return;
    }
    const source = accNoteDraggingId && panel.querySelector('[data-note-row][data-note-id="' + CSS.escape(accNoteDraggingId) + '"]');
    const target = event.target.closest('[data-note-row]');
    if (!source || (target === source) || (!target && !event.target.closest('[data-note-drop-parent]'))) return;
    event.preventDefault();
    const targetParentContainer = event.target.closest('[data-note-drop-parent]');
    let parentId = targetParentContainer ? Number(targetParentContainer.dataset.noteDropParent) : Number(target && target.parentElement.closest('[data-note-drop-parent]')?.dataset.noteDropParent || 0) || null;
    let group;
    let before = false;
    if (target && event.clientY > target.getBoundingClientRect().top + target.getBoundingClientRect().height * .34 && event.clientY < target.getBoundingClientRect().bottom - target.getBoundingClientRect().height * .34) {
        parentId = Number(target.dataset.noteId);
        group = target.querySelector(':scope > [data-note-drop-parent]');
    } else {
        group = target ? target.parentElement : targetParentContainer;
        if (target) before = event.clientY < target.getBoundingClientRect().top + target.getBoundingClientRect().height / 2;
    }
    if (!group) return;
    const ids = Array.from(group.children).filter(function (node) { return node.matches('[data-note-row]'); }).map(function (node) { return node.dataset.noteId; }).filter(function (id) { return id !== accNoteDraggingId; });
    const insertIndex = target && target.parentElement === group ? ids.indexOf(target.dataset.noteId) + (before ? 0 : 1) : ids.length;
    ids.splice(Math.max(0, insertIndex), 0, accNoteDraggingId);
    const status = panel.querySelector('[data-note-order-status]');
    panel.setAttribute('aria-busy', 'true');
    if (status && window.LoadingUI) window.LoadingUI.showSpinner(status, 'Saving note order', { escalate: false });
    try {
        await accApi('/api/account-control-center/categories/order', { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ client_id: accClientId, category_type: 'note', category_ids: ids.map(Number), parent_category_id: parentId }) });
        await accLoadNoteCategories();
        accShowStatus('Note breakdown order updated.', 'success');
    } finally {
        if (status && window.LoadingUI) window.LoadingUI.hideSpinner(status);
        panel.removeAttribute('aria-busy');
    }
}
function accSetCashflowConfigTab(tabName) {
    document.querySelectorAll('[data-acc-cashflow-config-tab]').forEach(function (tab) { tab.classList.toggle('active', tab.dataset.accCashflowConfigTab === tabName); });
    document.querySelectorAll('[data-acc-cashflow-config-panel]').forEach(function (panel) { panel.hidden = panel.dataset.accCashflowConfigPanel !== tabName; });
}
function accSetCashflowModal(id, open) { const modal = accElement(id); if (modal) { modal.hidden = !open; if (open) accHideStatus(accElement('accStatus')); } }
function accSetFinancialSetupModal(isOpen, entryMode) {
    const modal = accElement('accFinancialSetupModal');
    if (!modal) return;
    modal.hidden = !isOpen;
    if (isOpen) {
        const mode = entryMode === 'notes' ? 'notes' : 'financial';
        modal.dataset.entryMode = mode;
        modal.dataset.returnFocusId = mode === 'notes' ? 'accNotesSetupFab' : 'accFinancialSetupFab';
        accElement('accFinancialSetupModalTitle').textContent = mode === 'notes' ? 'Notes to Financial Statements' : 'Financial Statements';
        accHideStatus(accElement('accStatus'));
        accRenderFinancialOutlines();
        accSetFinancialSetupTab(mode === 'notes' ? 'notes-balance-sheet' : 'balance-sheet');
        accElement('accFinancialSetupClose').focus();
        accLoadNotesSetupData().catch(function (error) { accShowStatus(error.message, 'error'); });
    } else {
        accElement(modal.dataset.returnFocusId || 'accFinancialSetupFab').focus();
    }
}
function accSetFinancialSetupTab(tabName) {
    document.querySelectorAll('[data-acc-financial-setup-tab]').forEach(function (tab) {
        const active = tab.dataset.accFinancialSetupTab === tabName;
        tab.classList.toggle('active', active);
        tab.setAttribute('aria-selected', active ? 'true' : 'false');
    });
    document.querySelectorAll('[id^="accFinancialSetupPanel"]').forEach(function (panel) {
        panel.hidden = panel.getAttribute('aria-labelledby') !== document.querySelector('[data-acc-financial-setup-tab="' + CSS.escape(tabName) + '"]').id;
    });
}
function accSetTabLoading(tabName, isLoading) {
    const tab = document.querySelector('[data-acc-tab="' + CSS.escape(tabName) + '"]');
    if (!tab) return;
    tab.disabled = isLoading;
    tab.setAttribute('aria-busy', isLoading ? 'true' : 'false');
}
function accLoadLazyTab(tabName, message, loader, setError) {
    if (accTabLoading[tabName]) return;
    accTabLoading[tabName] = true;
    accSetTabLoading(tabName, true);
    accWithBusy(message, loader).catch(function (error) {
        setError(error.message, true);
    }).finally(function () {
        accTabLoading[tabName] = false;
        accSetTabLoading(tabName, false);
    });
}
function accSetTab(tabName) {
    document.querySelectorAll('[data-acc-tab]').forEach(function (tab) {
        const active = tab.dataset.accTab === tabName;
        tab.classList.toggle('active', active);
        tab.setAttribute('aria-selected', active ? 'true' : 'false');
    });
    accElement('accVerificationPanel').hidden = tabName !== 'verification';
    accElement('accCashflowPanel').hidden = tabName !== 'cashflow';
    accElement('accFinancialPanel').hidden = tabName !== 'financial';
    accElement('accNotesPanel').hidden = tabName !== 'notes';
    document.querySelectorAll('[data-acc-toolbar-fields]').forEach(function (toolbar) {
        toolbar.hidden = toolbar.dataset.accToolbarFields !== tabName;
    });
    document.querySelectorAll('[data-acc-toolbar-actions]').forEach(function (toolbar) {
        toolbar.hidden = toolbar.dataset.accToolbarActions !== tabName;
    });
    accElement('accCashflowCategoryFab').hidden = tabName !== 'cashflow';
    accElement('accFinancialSetupFab').hidden = tabName !== 'financial';
    accElement('accNotesSetupFab').hidden = tabName !== 'notes';
    if (tabName === 'cashflow' && !accCashflowLoaded) {
        accSetCashflowGridState('Loading Cash Flow Setup...', false, true);
        accLoadLazyTab('cashflow', 'Loading Cash Flow Setup...', accLoadCashflow, accSetCashflowGridState);
    }
    if (tabName === 'financial' && !accFinancialLoaded) {
        accSetFinancialGridState('Loading Financial Statement categories...', false, true);
        accLoadLazyTab('financial', 'Loading Financial Statement categories...', accLoadFinancialCategories, accSetFinancialGridState);
    }
    if (tabName === 'notes') {
        if (accNotesLoaded) accRenderNotesRows();
        else {
            accSetNotesGridState('Loading Financial Statement categories and note choices...', false, true);
            accLoadLazyTab('notes', 'Loading Financial Statement categories and note choices...', accLoadNotesSetupData, accSetNotesGridState);
        }
    }
}
function accSetTreasuryGuidance(isOpen) {
    const toggle = accElement('accTreasuryGuidanceToggle');
    const guidance = accElement('accTreasuryGuidance');
    if (!toggle || !guidance) return;
    guidance.hidden = !isOpen;
    toggle.setAttribute('aria-expanded', isOpen ? 'true' : 'false');
}
function accBindTreasuryGuidance() {
    const toggle = accElement('accTreasuryGuidanceToggle');
    const close = accElement('accTreasuryGuidanceClose');
    const guidance = accElement('accTreasuryGuidance');
    if (!toggle || !close || !guidance) return;
    toggle.addEventListener('click', function () { accSetTreasuryGuidance(guidance.hidden); });
    close.addEventListener('click', function () { accSetTreasuryGuidance(false); toggle.focus(); });
    document.addEventListener('keydown', function (event) { if (event.key === 'Escape' && !guidance.hidden) { accSetTreasuryGuidance(false); toggle.focus(); } });
}
function accBindEvents() {
    accBindTreasuryGuidance();
    accElement('accTrialBalanceSelect').addEventListener('change', async function (event) {
        accSelectedUploadId = event.target.value || null;
        accInvalidateTrialBalanceCache();
        try {
            await accWithBusy('Loading selected Trial Balance...', async function () {
                await accLoadMapping(true);
                if (accCashflowLoaded) await accLoadCashflow();
            });
        } catch (error) { accShowStatus(error.message, 'error'); }
    });
    accEnsureCashflowComboboxControls();
    document.addEventListener('input', accHandleCashflowComboboxInput);
    document.querySelectorAll('[data-acc-tab]').forEach(function (tab) { tab.addEventListener('click', function () { accSetTab(tab.dataset.accTab); }); });
    accElement('accCashflowCategoryFab').addEventListener('click', function () { accSetCashflowModal('accCashflowCategoryModal', true); accElement('accCashflowCategoryLabel').focus(); });
    accElement('accAccountFilter').addEventListener('input', accRenderRows);
    accElement('accAccountRows').addEventListener('change', function (event) { accHandleWorkstreamChange(event); });
    accElement('accCashflowFilter').addEventListener('input', accRenderCashflowRows);
    accElement('accCashflowRows').addEventListener('change', function (event) { accHandleCashflowChange(event); });
    accElement('accCashflowCategoryButton').addEventListener('click', function () { accSetCashflowModal('accCashflowCategoryModal', true); });
    accElement('accCashflowCategoryClose').addEventListener('click', function () { accSetCashflowModal('accCashflowCategoryModal', false); });
    accElement('accCashflowCategoryForm').addEventListener('submit', accCreateCashflowCategory);
    accElement('accCashflowCategoryList').addEventListener('click', accHandleCashflowCategoryAction);
    accElement('accCashflowAccountsButton').addEventListener('click', function () { accSetCashflowModal('accCashflowAccountsModal', true); accSetCashflowConfigTab('cash'); accRenderCashflowAccountOptions(); });
    accElement('accCashflowAccountsClose').addEventListener('click', function () { accSetCashflowModal('accCashflowAccountsModal', false); });
    document.querySelectorAll('[data-acc-cashflow-config-tab]').forEach(function (tab) { tab.addEventListener('click', function () { accSetCashflowConfigTab(tab.dataset.accCashflowConfigTab); }); });
    accElement('accCashflowCashSearch').addEventListener('input', accRenderCashflowAccountOptions);
    accElement('accCashflowBankSearch').addEventListener('input', accRenderCashflowAccountOptions);
    accElement('accCashflowCashOptions').addEventListener('change', accUpdateCashflowTreasurySelection);
    accElement('accCashflowBankOptions').addEventListener('change', accUpdateCashflowTreasurySelection);
    accElement('accCashflowCashSelected').addEventListener('click', accRemoveCashflowTreasurySelection);
    accElement('accCashflowBankSelected').addEventListener('click', accRemoveCashflowTreasurySelection);
    accElement('accCashflowAccountsSave').addEventListener('click', function (event) { accSaveCashflowTreasury(event.currentTarget).catch(function (error) { accShowStatus(error.message, 'error'); }); });
    accElement('accCashflowSettingsSave').addEventListener('click', function (event) { accSaveCashflowSettings(event.currentTarget).catch(function (error) { accShowStatus(error.message, 'error'); }); });
    accElement('accCashflowCustomerSettingsSave').addEventListener('click', function (event) { accSaveCashflowSettings(event.currentTarget).catch(function (error) { accShowStatus(error.message, 'error'); }); });
    accElement('accCashflowTemplateButton').addEventListener('click', function () { accOpenCashflowTemplateModal(); });
    accElement('accCashflowTemplateClose').addEventListener('click', function () { accSetCashflowModal('accCashflowTemplateModal', false); });
    accElement('accCashflowTemplateSelect').addEventListener('change', function (event) { accLoadCashflowTemplateMapping(event.target.value).catch(function (error) { accSetCashflowTemplateState(error.message, true); }); });
    accElement('accCashflowTemplateMapping').addEventListener('change', accHandleCashflowTemplateMappingChange);
    accElement('accCashflowTemplateSave').addEventListener('click', function () { accSaveCashflowTemplate().catch(function (error) { accSetCashflowTemplateState(error.message, true); }); });
    document.addEventListener('keydown', function (event) { if (event.key === 'Escape') { accSetCashflowModal('accCashflowCategoryModal', false); accSetCashflowModal('accCashflowAccountsModal', false); accSetCashflowModal('accCashflowTemplateModal', false); if (!accElement('accFinancialSetupModal').hidden) accSetFinancialSetupModal(false); } });
    accElement('accFinancialFilterLoad').addEventListener('click', function () {
        accAppliedFinancialAccountFilter = accElement('accFinancialFilter').value;
        accAppliedFinancialCategoryFilter = accElement('accFinancialCategoryFilter').value;
        accRenderFinancialRows();
    });
    const financialRows = accElement('accFinancialRows');
    financialRows.addEventListener('change', function (event) { accHandleFinancialChange(event); });
    financialRows.addEventListener('click', accHandleFinancialPickerClick);
    financialRows.addEventListener('input', accHandleFinancialPickerInput);
    financialRows.addEventListener('keydown', accHandleFinancialPickerKeydown);
    const notesRows = accElement('accNotesRows');
    notesRows.addEventListener('change', accHandleNoteChange);
    notesRows.addEventListener('click', accHandleFinancialPickerClick);
    notesRows.addEventListener('input', accHandleFinancialPickerInput);
    notesRows.addEventListener('keydown', accHandleFinancialPickerKeydown);
    document.addEventListener('click', function (event) { if (!event.target.closest('[data-fs-picker]')) accCloseFinancialPickers(); });
    document.addEventListener('scroll', function (event) { if (!event.target.closest || !event.target.closest('[data-fs-picker-menu]')) accCloseFinancialPickers(); }, true);
    window.addEventListener('resize', function () { accCloseFinancialPickers(); });
    accElement('accFinancialSetupFab').addEventListener('click', function () { accSetFinancialSetupModal(true, 'financial'); });
    accElement('accNotesSetupFab').addEventListener('click', function () { accSetFinancialSetupModal(true, 'notes'); });
    accElement('accFinancialSetupClose').addEventListener('click', function () { accSetFinancialSetupModal(false); });
    document.querySelectorAll('[data-acc-financial-setup-tab]').forEach(function (tab) { tab.addEventListener('click', function () { accSetFinancialSetupTab(tab.dataset.accFinancialSetupTab); }); });
    document.querySelectorAll('[data-fs-section]').forEach(function (panel) {
        panel.addEventListener('click', function (event) { accHandleFinancialCalculationAction(event).catch(function (error) { accShowStatus(error.message, 'error'); }); });
        panel.addEventListener('change', accHandleCalculationIncludeChange);
        panel.addEventListener('click', function (event) { accHandleFinancialOutlineAction(event).catch(function (error) { accShowStatus(error.message, 'error'); }); });
        panel.addEventListener('dragstart', accHandleFinancialDragStart);
        panel.addEventListener('dragover', accHandleFinancialDragOver);
        panel.addEventListener('drop', function (event) {
            accHandleFinancialDrop(event).catch(function (error) { accShowStatus(error.message, 'error'); accLoadFinancialCategories().catch(function () {}); }).finally(function () { accClearFinancialDropState(panel); accFinancialDraggingId = null; });
        });
        panel.addEventListener('dragend', function () { accClearFinancialDropState(panel); accFinancialDraggingId = null; });
    });
    accElement('accNotesFilterLoad').addEventListener('click', function () {
        accAppliedNotesAccountFilter = accElement('accNotesFilter').value;
        accAppliedNotesCategoryFilter = accElement('accNotesCategoryFilter').value;
        accRenderNotesRows();
    });
    document.querySelectorAll('[data-notes-section]').forEach(function (panel) {
        panel.addEventListener('click', function (event) { accHandleNotesOutlineAction(event).catch(function (error) { accShowStatus(error.message, 'error'); }); });
        panel.addEventListener('dragstart', accHandleNotesDragStart);
        panel.addEventListener('dragover', function (event) { if ((accNoteDraggingId || accNoteDraggingAnchorId) && event.target.closest('[data-note-row], [data-note-drop-parent], [data-note-source-drop]')) { event.preventDefault(); if (event.dataTransfer) event.dataTransfer.dropEffect = 'move'; } });
        panel.addEventListener('drop', function (event) { accHandleNotesDrop(event).catch(function (error) { accShowStatus(error.message, 'error'); accLoadNoteCategories().catch(function () {}); }).finally(function () { panel.querySelectorAll('.acc2-note-dragging').forEach(function (row) { row.classList.remove('acc2-note-dragging'); }); accNoteDraggingId = null; accNoteDraggingAnchorId = null; }); });
        panel.addEventListener('dragend', function () { panel.querySelectorAll('.acc2-note-dragging').forEach(function (row) { row.classList.remove('acc2-note-dragging'); }); accNoteDraggingId = null; accNoteDraggingAnchorId = null; });
    });
}
async function initAccountControlCentre() {
    await accWithBusy('Loading Account Control Centre...', async function () {
        if (window.clientContextReady) await window.clientContextReady;
        accBindEvents();
        accClientId = window.getSelectedClientId ? window.getSelectedClientId() : null;
        if (!accClientId) { accSetGridState('Select a client before opening Account Control Centre.', true); return; }
        accIsAdmin = await window.isAdminUser();
        if (!accIsAdmin) accElement('accAdminWarning').hidden = false;
        accSetGridState('Loading Trial Balance...', false, true);
        try { await accLoadMapping(); } catch (error) { accSetGridState(error.message, true); }
    });
}
document.addEventListener('DOMContentLoaded', initAccountControlCentre);
