// Simplified Account Control Centre: Verification and Financial Statement mappings.
let accClientId = null;
let accAccounts = [];
let accHierarchyIndex = { accountByCode: new Map(), ancestorsByAccount: new WeakMap(), descendantsByCode: new Map(), leafDescendantsByCode: new Map() };
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
let accDetailLoaded = { verification: false, cashflow: false, financial: false, notes: false };
let accUploadLocks = 0;
const accStatusTimers = new WeakMap();
const ACC_STATUS_DURATION_MS = 4200;

const ACC_WORKSTREAMS = AccountAssignments.workstreams;
function accElement(id) { return document.getElementById(id); }
function accEscape(value) {
    return String(value == null ? '' : value).replace(/[&<>'"]/g, function (character) {
        return ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;' })[character];
    });
}
function accNormaliseCode(value) { return AccountAssignments.normaliseCode(value); }
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
// Build code and descendant indexes once per Trial Balance refresh. Assignment
// values stay on the account objects so edits are reflected without rebuilding.
function accRebuildHierarchyIndex() {
    const accountByCode = new Map();
    accAccounts.forEach(function (account) {
        const code = accNormaliseCode(account.account_code);
        if (code && !accountByCode.has(code)) accountByCode.set(code, account);
    });
    const ancestorsByAccount = new WeakMap();
    const descendantsByCode = new Map();
    const leafDescendantsByCode = new Map();
    accAccounts.forEach(function (account) {
        const accountCode = accNormaliseCode(account.account_code);
        const codes = new Set();
        if (accountCode) codes.add(accountCode);
        let ancestor = accNormaliseCode(account.parent_account_code);
        const visited = new Set();
        while (ancestor && !visited.has(ancestor)) {
            codes.add(ancestor);
            visited.add(ancestor);
            const parent = accountByCode.get(ancestor);
            ancestor = parent ? accNormaliseCode(parent.parent_account_code) : '';
        }
        ancestorsByAccount.set(account, codes);
        codes.forEach(function (code) {
            const descendants = descendantsByCode.get(code) || [];
            descendants.push(account);
            descendantsByCode.set(code, descendants);
            if (account.is_leaf) {
                const leaves = leafDescendantsByCode.get(code) || [];
                leaves.push(account);
                leafDescendantsByCode.set(code, leaves);
            }
        });
    });
    accHierarchyIndex = { accountByCode: accountByCode, ancestorsByAccount: ancestorsByAccount, descendantsByCode: descendantsByCode, leafDescendantsByCode: leafDescendantsByCode };
}
function accSetAccounts(accounts) {
    const nextAccounts = Array.isArray(accounts) ? accounts : [];
    if (accAccounts === nextAccounts) return;
    accAccounts = nextAccounts;
    accRebuildHierarchyIndex();
}
function accAccountIsWithin(account, parentCode) {
    const selectedCode = accNormaliseCode(parentCode);
    if (!selectedCode) return false;
    const ancestors = accHierarchyIndex.ancestorsByAccount.get(account);
    return !!ancestors && ancestors.has(selectedCode);
}
function accLeavesBelow(parentCode) {
    return accHierarchyIndex.leafDescendantsByCode.get(accNormaliseCode(parentCode)) || [];
}
function accEffectiveWorkstream(account) {
    const direct = accAssignment(account).verification_workstream || '';
    if (account.is_leaf || direct) return direct;
    const values = Array.from(new Set(accLeavesBelow(account.account_code).map(function (leaf) {
        return accAssignment(leaf).verification_workstream || '';
    })));
    return values.length === 1 ? values[0] : (values.length > 1 ? '__mixed' : '');
}
function accEffectiveFinancialCategory(...args) { return accFsWorkspace.accEffectiveFinancialCategory(...args); }
function accEffectiveNoteCategory(...args) { return accFsWorkspace.accEffectiveNoteCategory(...args); }
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
            const ancestors = accHierarchyIndex.ancestorsByAccount.get(match);
            if (!ancestors) return;
            accAccounts.forEach(function (candidate) {
                if (!candidate.is_leaf && ancestors.has(accNormaliseCode(candidate.account_code))) visible.add(candidate);
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
function accCategoryOptionMarkupFor(...args) { return accFsWorkspace.accCategoryOptionMarkupFor(...args); }
function accCategoryOptionMarkup(...args) { return accFsWorkspace.accCategoryOptionMarkup(...args); }
function accCashflowCategoryOptionMarkup(selected) { return accCategoryOptionMarkupFor(accCashflowCategories, selected); }
function accFinancialPickerLabel(...args) { return accFsWorkspace.accFinancialPickerLabel(...args); }
function accFinancialPickerOptionMarkup(...args) { return accFsWorkspace.accFinancialPickerOptionMarkup(...args); }
function accFinancialPickerSectionMarkup(...args) { return accFsWorkspace.accFinancialPickerSectionMarkup(...args); }
function accFinancialPickerMenuMarkup(...args) { return accFsWorkspace.accFinancialPickerMenuMarkup(...args); }
function accFinancialPickerMarkup(...args) { return accFsWorkspace.accFinancialPickerMarkup(...args); }
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
    if (!accDetailLoaded.verification) return;
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
    const parent = accHierarchyIndex.accountByCode.get(accNormaliseCode(account.parent_account_code));
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
    if (!accDetailLoaded.cashflow) return;
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
    if (!accDetailLoaded.financial) return;
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
function accGetEffectiveNoteOptions(...args) { return accFsWorkspace.accGetEffectiveNoteOptions(...args); }
function accNotePickerLabel(...args) { return accFsWorkspace.accNotePickerLabel(...args); }
function accNoteAssignmentSearchText(...args) { return accFsWorkspace.accNoteAssignmentSearchText(...args); }
function accNotePickerMarkup(...args) { return accFsWorkspace.accNotePickerMarkup(...args); }
function accRenderNotesRows() {
    if (!accDetailLoaded.notes) return;
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
    select.innerHTML = accAvailableUploads.length ? (!upload ? '<option value="" selected>— Select a Trial Balance —</option>' : '') + accAvailableUploads.map(function (item) {
        return '<option value="' + item.id + '"' + (upload && String(item.id) === String(upload.id) ? ' selected' : '') + '>' + accEscape(label(item)) + '</option>';
    }).join('') : '<option value="">No parsed Trial Balances</option>';
    select.disabled = !accAvailableUploads.length || accUploadLocks > 0;
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
            (accHierarchyIndex.descendantsByCode.get(accountCode) || []).forEach(function (child) {
                visibleCodes.add(accNormaliseCode(child.account_code));
            });
        }
        const ancestors = accHierarchyIndex.ancestorsByAccount.get(account);
        if (ancestors) ancestors.forEach(function (code) { visibleCodes.add(code); });
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
    AccountAssignments.invalidate(accApi);
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
        accSetAccounts(accTrialBalanceCache.accounts);
        accLatestUpload = accTrialBalanceCache.upload;
        return;
    }
    if (accTrialBalanceCache.request && accTrialBalanceCache.clientId === clientId && accTrialBalanceCache.uploadId === uploadId) return accTrialBalanceCache.request;
    accTrialBalanceCache.clientId = clientId;
    accTrialBalanceCache.uploadId = uploadId;
    const request = AccountAssignments.read('mapping', accClientId, uploadId, accApi).then(function (result) {
        accTrialBalanceCache.accounts = result.accounts || [];
        accTrialBalanceCache.upload = result.upload || null;
        accTrialBalanceCache.uploads = result.uploads || accAvailableUploads;
        accAvailableUploads = accTrialBalanceCache.uploads;
        accSelectedUploadId = result.upload ? String(result.upload.id) : null;
        accTrialBalanceCache.uploadId = String(accSelectedUploadId || '');
        accSetAccounts(accTrialBalanceCache.accounts);
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
    const result = await AccountAssignments.read('cashflow', accClientId, accSelectedUploadId, accApi);
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
let accProfitLink = null;
function accLoadFinancialCategories(...args) { return accFsWorkspace.accLoadFinancialCategories(...args); }
function accProfitLinkMarkup(...args) { return accFsWorkspace.accProfitLinkMarkup(...args); }
function accHandleProfitLinkAction(...args) { return accFsWorkspace.accHandleProfitLinkAction(...args); }
// Presentation is explicit configuration, never inferred from a balance's sign.
function accPresentationMarkup(...args) { return accFsWorkspace.accPresentationMarkup(...args); }
function accHandlePresentationChange(...args) { return accFsWorkspace.accHandlePresentationChange(...args); }
function accRenderFinancialOutlines(...args) { return accFsWorkspace.accRenderFinancialOutlines(...args); }

function accFinancialTotalAnchorRow(...args) { return accFsWorkspace.accFinancialTotalAnchorRow(...args); }

function accFinancialCalculationEditorMarkup(...args) { return accFsWorkspace.accFinancialCalculationEditorMarkup(...args); }
function accUpdateFinancialCalculationPlacementPreview(...args) { return accFsWorkspace.accUpdateFinancialCalculationPlacementPreview(...args); }
function accUpdateFinancialTotalChoiceAvailability(...args) { return accFsWorkspace.accUpdateFinancialTotalChoiceAvailability(...args); }
function accFinancialSortRows(...args) { return accFsWorkspace.accFinancialSortRows(...args); }
function accClearFinancialDropState(...args) { return accFsWorkspace.accClearFinancialDropState(...args); }
function accHandleFinancialDragStart(...args) { return accFsWorkspace.accHandleFinancialDragStart(...args); }
function accHandleFinancialDragOver(...args) { return accFsWorkspace.accHandleFinancialDragOver(...args); }
function accHandleFinancialDrop(...args) { return accFsWorkspace.accHandleFinancialDrop(...args); }
function accFinancialKey(...args) { return accFsWorkspace.accFinancialKey(...args); }
function accOpenFinancialCalculationEditor(...args) { return accFsWorkspace.accOpenFinancialCalculationEditor(...args); }
function accHandleFinancialCalculationAction(...args) { return accFsWorkspace.accHandleFinancialCalculationAction(...args); }
function accHandleCalculationIncludeChange(...args) { return accFsWorkspace.accHandleCalculationIncludeChange(...args); }
function accHandleFinancialOutlineAction(...args) { return accFsWorkspace.accHandleFinancialOutlineAction(...args); }
function accRenderNotesOutlines(...args) { return accFsWorkspace.accRenderNotesOutlines(...args); }
function accLoadNoteCategories(...args) { return accFsWorkspace.accLoadNoteCategories(...args); }
function accLoadNotesSetupData(...args) { return accFsWorkspace.accLoadNotesSetupData(...args); }
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
function accSaveFinancialLeaf(...args) { return accFsWorkspace.accSaveFinancialLeaf(...args); }
function accSaveNoteLeaf(...args) { return accFsWorkspace.accSaveNoteLeaf(...args); }
function accApplyNoteParent(...args) { return accFsWorkspace.accApplyNoteParent(...args); }
function accApplyFinancialParent(...args) { return accFsWorkspace.accApplyFinancialParent(...args); }
function accFilterFinancialPicker(...args) { return accFsWorkspace.accFilterFinancialPicker(...args); }
function accPositionFinancialPicker(...args) { return accFsWorkspace.accPositionFinancialPicker(...args); }
function accCloseFinancialPicker(...args) { return accFsWorkspace.accCloseFinancialPicker(...args); }
function accCloseFinancialPickers(...args) { return accFsWorkspace.accCloseFinancialPickers(...args); }
function accOpenFinancialPicker(...args) { return accFsWorkspace.accOpenFinancialPicker(...args); }
function accUpdateFinancialPickerSelection(...args) { return accFsWorkspace.accUpdateFinancialPickerSelection(...args); }
function accHandleFinancialPickerClick(...args) { return accFsWorkspace.accHandleFinancialPickerClick(...args); }
function accHandleFinancialPickerInput(...args) { return accFsWorkspace.accHandleFinancialPickerInput(...args); }
function accHandleFinancialPickerKeydown(...args) { return accFsWorkspace.accHandleFinancialPickerKeydown(...args); }
function accHandleFinancialChange(...args) { return accFsWorkspace.accHandleFinancialChange(...args); }
function accHandleNoteChange(...args) { return accFsWorkspace.accHandleNoteChange(...args); }
function accUpdateNoteCategory(...args) { return accFsWorkspace.accUpdateNoteCategory(...args); }
function accHandleNotesOutlineAction(...args) { return accFsWorkspace.accHandleNotesOutlineAction(...args); }
function accNoteSubtreeAssignedCount(...args) { return accFsWorkspace.accNoteSubtreeAssignedCount(...args); }
function accHandleNotesDragStart(...args) { return accFsWorkspace.accHandleNotesDragStart(...args); }
function accHandleNotesDrop(...args) { return accFsWorkspace.accHandleNotesDrop(...args); }
function accSetCashflowConfigTab(tabName) {
    document.querySelectorAll('[data-acc-cashflow-config-tab]').forEach(function (tab) { tab.classList.toggle('active', tab.dataset.accCashflowConfigTab === tabName); });
    document.querySelectorAll('[data-acc-cashflow-config-panel]').forEach(function (panel) { panel.hidden = panel.dataset.accCashflowConfigPanel !== tabName; });
}
function accSetCashflowModal(id, open) { const modal = accElement(id); if (modal) { modal.hidden = !open; if (open) accHideStatus(accElement('accStatus')); } }
function accSetFinancialSetupModal(...args) { return accFsWorkspace.accSetFinancialSetupModal(...args); }
function accSetFinancialSetupTab(...args) { return accFsWorkspace.accSetFinancialSetupTab(...args); }
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
}

// The selected upload cannot change while a detail request or setup load is in flight.
async function accWithUploadLock(operation) {
    accUploadLocks++;
    accElement('accTrialBalanceSelect').disabled = true;
    try { return await operation(); }
    finally {
        accUploadLocks--;
        accElement('accTrialBalanceSelect').disabled = accUploadLocks > 0 || !accAvailableUploads.length;
    }
}

function accResetDetailViews() {
    accDetailLoaded = {verification: false, cashflow: false, financial: false, notes: false};
    accSetAccounts([]);
    accCashflowLoaded = false;
    accCashflowHistoricalOrphans = [];
    accElement('accCashflowOrphanNotice').hidden = true;
    [accSetGridState, accSetCashflowGridState, accSetFinancialGridState, accSetNotesGridState].forEach(function (setState) {
        setState('Select a Trial Balance and click Load to view accounts.', false, false);
    });
}

async function accLoadDetail(tabName, button) {
    const states = {verification: accSetGridState, cashflow: accSetCashflowGridState, financial: accSetFinancialGridState, notes: accSetNotesGridState};
    const renders = {verification: accRenderRows, cashflow: accRenderCashflowRows, financial: accRenderFinancialRows, notes: accRenderNotesRows};
    if (!accSelectedUploadId) { states[tabName]('Select a Trial Balance before loading accounts.', true); return; }
    if (button.disabled) return;
    states[tabName]('Loading accounts...', false, true);
    try {
        await accWithUploadLock(function () { return accWithButtonBusy(button, 'Loading...', async function () {
            await accLoadTrialBalance(false);
            if (tabName === 'cashflow' && !accCashflowLoaded) await accLoadCashflow();
            if (tabName === 'financial') await accLoadNotesSetupData();
            if (tabName === 'notes') await accLoadNotesSetupData();
            accDetailLoaded[tabName] = true;
            renders[tabName]();
        }); });
    } catch (error) {
        accDetailLoaded[tabName] = false;
        states[tabName](error.message + ' Click Load to retry.', true);
    }
}

async function accOpenLoadedSetup(button, loader, open) {
    if (!accSelectedUploadId) { accShowStatus('Select a Trial Balance first.', 'error'); return; }
    if (button.disabled) return;
    try {
        await accWithUploadLock(function () { return accWithButtonBusy(button, 'Loading...', loader); });
        open();
    } catch (error) { accShowStatus(error.message + ' Try again.', 'error'); }
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
    document.querySelectorAll('[data-acc-trial-balance-link]').forEach(function (button) {
        button.addEventListener('click', function () {
            const tab = button.closest('[data-acc-toolbar-actions]').dataset.accToolbarActions;
            window.location.href = AccountAssignments.trialBalanceUrl(tab, accSelectedUploadId, new URLSearchParams(location.search).get('from') === 'trial-balance');
        });
    });
    accElement('accTrialBalanceSelect').addEventListener('change', function (event) {
        accSelectedUploadId = event.target.value || null;
        accLatestUpload = accAvailableUploads.find(function (upload) { return String(upload.id) === accSelectedUploadId; }) || null;
        accInvalidateTrialBalanceCache();
        accResetDetailViews();
    });
    accEnsureCashflowComboboxControls();
    document.addEventListener('input', accHandleCashflowComboboxInput);
    document.querySelectorAll('[data-acc-tab]').forEach(function (tab) { tab.addEventListener('click', function () { accSetTab(tab.dataset.accTab); }); });
    accElement('accVerificationLoad').addEventListener('click', function () { accLoadDetail('verification', this); });
    accElement('accCashflowLoad').addEventListener('click', function () { accLoadDetail('cashflow', this); });
    accElement('accCashflowCategoryFab').addEventListener('click', function () { accOpenLoadedSetup(this, function () { return accCashflowLoaded ? Promise.resolve() : accLoadCashflow(); }, function () { accSetCashflowModal('accCashflowCategoryModal', true); accElement('accCashflowCategoryLabel').focus(); }); });
    accElement('accAccountFilter').addEventListener('input', accRenderRows);
    accElement('accAccountRows').addEventListener('change', function (event) { accHandleWorkstreamChange(event); });
    accElement('accCashflowFilter').addEventListener('input', accRenderCashflowRows);
    accElement('accCashflowRows').addEventListener('change', function (event) { accHandleCashflowChange(event); });
    accElement('accCashflowCategoryButton').addEventListener('click', function () { accOpenLoadedSetup(this, function () { return accCashflowLoaded ? Promise.resolve() : accLoadCashflow(); }, function () { accSetCashflowModal('accCashflowCategoryModal', true); }); });
    accElement('accCashflowCategoryClose').addEventListener('click', function () { accSetCashflowModal('accCashflowCategoryModal', false); });
    accElement('accCashflowCategoryForm').addEventListener('submit', accCreateCashflowCategory);
    accElement('accCashflowCategoryList').addEventListener('click', accHandleCashflowCategoryAction);
    accElement('accCashflowAccountsButton').addEventListener('click', function () { accOpenLoadedSetup(this, function () { return accCashflowLoaded ? Promise.resolve() : accLoadCashflow(); }, function () { accSetCashflowModal('accCashflowAccountsModal', true); accSetCashflowConfigTab('cash'); accRenderCashflowAccountOptions(); }); });
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
    accElement('accCashflowTemplateButton').addEventListener('click', function () { accOpenLoadedSetup(this, function () { return accCashflowLoaded ? Promise.resolve() : accLoadCashflow(); }, accOpenCashflowTemplateModal); });
    accElement('accCashflowTemplateClose').addEventListener('click', function () { accSetCashflowModal('accCashflowTemplateModal', false); });
    accElement('accCashflowTemplateSelect').addEventListener('change', function (event) { accLoadCashflowTemplateMapping(event.target.value).catch(function (error) { accSetCashflowTemplateState(error.message, true); }); });
    accElement('accCashflowTemplateMapping').addEventListener('change', accHandleCashflowTemplateMappingChange);
    accElement('accCashflowTemplateSave').addEventListener('click', function () { accSaveCashflowTemplate().catch(function (error) { accSetCashflowTemplateState(error.message, true); }); });
    document.addEventListener('keydown', function (event) { if (event.key === 'Escape') { accSetCashflowModal('accCashflowCategoryModal', false); accSetCashflowModal('accCashflowAccountsModal', false); accSetCashflowModal('accCashflowTemplateModal', false); } });
    accElement('accFinancialFilterLoad').addEventListener('click', function () {
        accAppliedFinancialAccountFilter = accElement('accFinancialFilter').value;
        accAppliedFinancialCategoryFilter = accElement('accFinancialCategoryFilter').value;
        accLoadDetail('financial', this);
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
    accElement('accFinancialSetupFab').addEventListener('click', function () { accOpenLoadedSetup(this, accLoadNotesSetupData, function () { accSetFinancialSetupModal(true, 'financial'); }); });
    accElement('accNotesSetupFab').addEventListener('click', function () { accOpenLoadedSetup(this, accLoadNotesSetupData, function () { accSetFinancialSetupModal(true, 'notes'); }); });
    accElement('accNotesFilterLoad').addEventListener('click', function () {
        accAppliedNotesAccountFilter = accElement('accNotesFilter').value;
        accAppliedNotesCategoryFilter = accElement('accNotesCategoryFilter').value;
        accLoadDetail('notes', this);
    });

}
async function accBootstrapUploads() {
    const overlay = accElement('accBootstrapOverlay');
    overlay.hidden = false;
    accElement('accRetryBootstrap').hidden = true;
    try {
        const result = await accApi('/api/account-control-center/uploads?client_id=' + encodeURIComponent(accClientId));
        accAvailableUploads = result.uploads || [];
        const requested = new URLSearchParams(window.location.search).get('upload_id');
        const defaultId = result.default_upload_id == null ? null : String(result.default_upload_id);
        const chosen = requested ? accAvailableUploads.find(function (upload) { return String(upload.id) === requested; })
            : accAvailableUploads.find(function (upload) { return String(upload.id) === defaultId; }) || accAvailableUploads[0];
        accLatestUpload = chosen || null;
        accSelectedUploadId = chosen ? String(chosen.id) : null;
        accInvalidateTrialBalanceCache();
        accResetDetailViews();
        accRenderSource();
        if (requested && !chosen) accShowStatus('The requested Trial Balance is not available for this client. Select another upload.', 'error');
    } catch (error) {
        accAvailableUploads = []; accSelectedUploadId = null; accLatestUpload = null;
        accResetDetailViews(); accRenderSource();
        accElement('accRetryBootstrap').hidden = false;
        accShowStatus('Could not load Trial Balances: ' + error.message, 'error');
    } finally { overlay.hidden = true; }
}
// Setup links reuse the normal loaders, permission checks and modal handlers.
async function accOpenRequestedSetup() {
    if (!accIsAdmin || !accSelectedUploadId) return;
    const params = new URLSearchParams(location.search);
    const action = params.get('setup');
    const mode = AccountAssignments.mode(params.get('tab') || 'verification');
    if (!AccountAssignments.workflows[mode].setups.some(item => item[0] === action)) return;
    const buttons = {financial: 'accFinancialSetupFab', notes: 'accNotesSetupFab', cashflow: 'accCashflowCategoryFab', treasury: 'accCashflowAccountsButton', template: 'accCashflowTemplateButton'};
    if (action === 'verification') {
        await accLoadDetail('verification', accElement('accVerificationLoad'));
    } else if (Object.hasOwn(buttons, action)) {
        accElement(buttons[action]).click();
    }
}
async function initAccountControlCentre() {
    try {
        if (window.clientContextReady) await window.clientContextReady;
        accBindEvents();
        accElement('accRetryBootstrap').addEventListener('click', accBootstrapUploads);
        const tab = new URLSearchParams(window.location.search).get('tab');
        if (['financial', 'notes', 'cashflow', 'verification'].includes(tab)) accSetTab(tab);
        accClientId = window.getSelectedClientId ? window.getSelectedClientId() : null;
        if (!accClientId) { accElement('accTrialBalanceSelect').disabled = true; accSetGridState('Select a client before opening Account Control Centre.', true); return; }
        accIsAdmin = await window.isAdminUser();
        if (!accIsAdmin) { accElement('accAdminWarning').hidden = false; accElement('accTrialBalanceSelect').disabled = true; return; }
        await accBootstrapUploads();
        await accOpenRequestedSetup();
    } finally { accElement('accBootstrapOverlay').hidden = true; }
}
const accFsWorkspace = FinancialStatementWorkspace.create({
    state: {
    get categories() { return accFinancialCategories; }, set categories(value) { accFinancialCategories = value; },
    get calculations() { return accFinancialCalculations; }, set calculations(value) { accFinancialCalculations = value; },
    get editor() { return accFinancialCalculationEditor; }, set editor(value) { accFinancialCalculationEditor = value; },
    get notes() { return accNoteCategories; }, set notes(value) { accNoteCategories = value; },
    get financialLoaded() { return accFinancialLoaded; }, set financialLoaded(value) { accFinancialLoaded = value; },
    get notesLoaded() { return accNotesLoaded; }, set notesLoaded(value) { accNotesLoaded = value; },
    get dragging() { return accFinancialDraggingId; }, set dragging(value) { accFinancialDraggingId = value; },
    get noteDragging() { return accNoteDraggingId; }, set noteDragging(value) { accNoteDraggingId = value; },
    get noteAnchorDragging() { return accNoteDraggingAnchorId; }, set noteAnchorDragging(value) { accNoteDraggingAnchorId = value; },
    get profitLink() { return accProfitLink; }, set profitLink(value) { accProfitLink = value; }
    },
    get accounts() { return accAccounts; }, get clientId() { return accClientId; }, get isAdmin() { return accIsAdmin; },
    leavesBelow: accLeavesBelow, uploadPayload: accSelectedUploadPayload, api: accApi,
    refreshMapping: accLoadMapping, renderFinancialRows: accRenderFinancialRows, renderNotesRows: accRenderNotesRows,
    showStatus: accShowStatus, clearStatus: accClearStatus, withBusy: accWithBusy,
    focusTarget: mode => accElement(mode === 'notes' ? 'accNotesSetupFab' : 'accFinancialSetupFab'),
    assignmentBusy: async function(kind,parent,account,select,operation) {
        const id=kind==='financial'?'accFinancialRows':'accNotesRows';
        const marker=kind==='financial'?'data-acc-fs-result':'data-acc-note-result';
        const result=accElement(id)?.querySelector('['+marker+'="'+CSS.escape(account.account_code)+'"]');
        accSetInlineBusy(select,result,'Saving…');let saved=false;
        try {
            const value=parent?await accWithTableBusy(kind==='financial'?'accFinancialGridWrap':'accNotesGridWrap','Applying assignment…',operation)
                :kind==='financial'?await operation():await accWithBusy('Saving note assignment…',operation);
            saved=true;return value;
        } finally {accSetInlineResult(select,result,saved);}
    },
});
document.addEventListener('DOMContentLoaded', initAccountControlCentre);
