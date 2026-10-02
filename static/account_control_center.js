// Simplified Account Control Centre: Verification and Financial Statement mappings.
let accClientId = null;
let accAccounts = [];
let accLatestUpload = null;
let accTrialBalanceCache = { clientId: null, accounts: null, upload: null, request: null };
let accFinancialCategories = [];
let accNoteCategories = [];
let accCashflowCategories = [];
let accCashflowHistoricalOrphans = [];
let accCashflowTreasury = { cash: [], bank: [] };
let accCashflowSettings = { match_internal_transfers: false, receipt_presentation: 'net', source_template_id: null, role_categories: { customer_receipt: [], bank_charge: [], withholding_tax: [], other_deduction: [] } };
let accCashflowTemplateMapping = null;
let accFinancialLoaded = false;
let accNotesLoaded = false;
let accCashflowLoaded = false;
let accIsAdmin = false;
let accBusyOperations = new Map();
let accBusySequence = 0;
let accTabLoading = { cashflow: false, financial: false, notes: false };

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
function accShowStatus(message, type) {
    const status = accElement('accStatus');
    status.textContent = message;
    status.className = 'acc2-status' + (type ? ' ' + type : '');
    status.hidden = false;
}
function accClearStatus() { accElement('accStatus').hidden = true; }
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
function accLeavesBelow(parentCode) {
    const prefix = accNormaliseCode(parentCode) + '-';
    return accAccounts.filter(function (account) {
        const code = accNormaliseCode(account.account_code);
        return account.is_leaf && (code === accNormaliseCode(parentCode) || code.indexOf(prefix) === 0);
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
    const values = Array.from(new Set(accLeavesBelow(account).map(function (leaf) {
        const value = accAssignment(leaf).financial_statement_category_id;
        return value == null ? '' : String(value);
    })));
    return values.length === 1 ? values[0] : (values.length > 1 ? '__mixed' : '');
}
function accEffectiveNoteCategory(account) {
    const direct = accAssignment(account).note_category_id;
    if (account.is_leaf || direct) return direct == null ? '' : String(direct);
    const values = Array.from(new Set(accLeavesBelow(account).map(function (leaf) {
        const value = accAssignment(leaf).note_category_id;
        return value == null ? '' : String(value);
    })));
    return values.length === 1 ? values[0] : (values.length > 1 ? '__mixed' : '');
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
    html += categories.map(function (category) {
        const value = String(category.id);
        const inactive = !category.is_active;
        const label = (inactive ? 'Inactive: ' : '') + category.label;
        return '<option value="' + accEscape(value) + '"' + (value === String(selected) ? ' selected' : '') + (inactive && value !== String(selected) ? ' disabled' : '') + '>' + accEscape(label) + '</option>';
    }).join('');
    return html;
}
function accCategoryOptionMarkup(selected) { return accCategoryOptionMarkupFor(accFinancialCategories, selected); }
function accNoteCategoryOptionMarkup(selected) { return accCategoryOptionMarkupFor(accNoteCategories, selected); }
function accCashflowCategoryOptionMarkup(selected) { return accCategoryOptionMarkupFor(accCashflowCategories, selected); }
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
    const filter = String(accElement('accFinancialFilter').value || '').trim().toLowerCase();
    const visible = accAccounts.filter(function (account) {
        return !filter || (String(account.account_code || '') + ' ' + String(account.account_name || '')).toLowerCase().indexOf(filter) !== -1;
    });
    accElement('accFinancialCount').textContent = visible.length + ' of ' + accAccounts.length + ' accounts';
    const rows = accElement('accFinancialRows');
    if (!visible.length) {
        rows.innerHTML = '<tr><td colspan="5" class="acc2-empty">No accounts match this search.</td></tr>';
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
                '<td><select class="acc2-financial-category" data-acc-code="' + accEscape(account.account_code) + '" data-acc-parent="' + (isParent ? 'true' : 'false') + '"' + (!accIsAdmin ? ' disabled' : '') + '>' + accCategoryOptionMarkup(selected) + '</select></td>' +
                '<td>' + result + '</td>' +
                '</tr>';
        }).join('');
    }
    accSetStateReady('accFinancialGridState');
    accElement('accFinancialGridWrap').hidden = false;
}
function accSetNotesGridState(message, isError, isLoading) { accSetState('accNotesGridState', 'accNotesGridWrap', message, isError, isLoading); }
function accRenderNotesRows() {
    const filter = String(accElement('accNotesFilter').value || '').trim().toLowerCase();
    const visible = accAccounts.filter(function (account) {
        return !filter || (String(account.account_code || '') + ' ' + String(account.account_name || '')).toLowerCase().indexOf(filter) !== -1;
    });
    accElement('accNotesCount').textContent = visible.length + ' of ' + accAccounts.length + ' accounts';
    const rows = accElement('accNotesRows');
    if (!visible.length) {
        rows.innerHTML = '<tr><td colspan="5" class="acc2-empty">No accounts match this search.</td></tr>';
    } else {
        rows.innerHTML = visible.map(function (account) {
            const isParent = !account.is_leaf;
            const selected = accEffectiveNoteCategory(account);
            const indent = Math.max(0, Number(account.level || 1) - 1) * 16;
            const result = isParent ? '<span class="acc2-parent-help">Applies to children</span>' : '<span class="acc2-saved" data-acc-note-result="' + accEscape(account.account_code) + '">Saved</span>';
            return '<tr class="' + (isParent ? 'acc2-parent' : '') + '">' +
                '<td class="acc2-code" style="padding-left:' + (indent + 10) + 'px">' + accEscape(account.account_code) + '</td>' +
                '<td>' + accEscape(account.account_name || 'Unknown account') + '</td>' +
                '<td><span class="acc2-type ' + (isParent ? 'parent' : 'leaf') + '">' + (isParent ? 'Parent' : 'Leaf') + '</span></td>' +
                '<td><select class="acc2-note-category" data-acc-code="' + accEscape(account.account_code) + '" data-acc-parent="' + (isParent ? 'true' : 'false') + '"' + (!accIsAdmin ? ' disabled' : '') + '>' + accNoteCategoryOptionMarkup(selected) + '</select></td>' +
                '<td>' + result + '</td>' +
                '</tr>';
        }).join('');
    }
    accSetStateReady('accNotesGridState');
    accElement('accNotesGridWrap').hidden = false;
}
function accRenderSource() {
    const upload = accLatestUpload;
    accElement('accSourceInfo').textContent = upload ? 'Source: ' + (upload.file_name || 'Latest Trial Balance') + (upload.as_of_date ? ' - ' + upload.as_of_date : '') : 'No parsed Trial Balance';
}
function accRenderFinancialCategoryList() {
    const list = accElement('accFinancialCategoryList');
    if (!accFinancialCategories.length) {
        list.innerHTML = '<span class="acc2-empty">No Financial Statement categories defined yet.</span>';
        return;
    }
    list.innerHTML = accFinancialCategories.map(function (category, index) {
        const status = category.is_active ? 'Active' : 'Inactive';
        const action = category.is_active ? 'Deactivate' : 'Reactivate';
        const upDisabled = index === 0 ? ' disabled' : '';
        const downDisabled = index === accFinancialCategories.length - 1 ? ' disabled' : '';
        return '<div class="acc2-category-item"><div><strong>' + accEscape(category.label) + '</strong><span>' + accEscape(category.category_key) + ' - ' + status + '</span></div><div class="acc2-category-actions"><button type="button" class="acc2-mini-action" data-acc-category-id="' + category.id + '" data-acc-category-action="rename">Rename</button><button type="button" class="acc2-mini-action" data-acc-category-id="' + category.id + '" data-acc-category-action="up"' + upDisabled + ' aria-label="Move ' + accEscape(category.label) + ' up">↑</button><button type="button" class="acc2-mini-action" data-acc-category-id="' + category.id + '" data-acc-category-action="down"' + downDisabled + ' aria-label="Move ' + accEscape(category.label) + ' down">↓</button><button type="button" class="acc2-mini-action" data-acc-category-id="' + category.id + '" data-acc-category-action="toggle" data-acc-category-active="' + (category.is_active ? 'true' : 'false') + '">' + action + '</button></div></div>';
    }).join('');
}
function accRenderNoteCategoryList() {
    const list = accElement('accNotesCategoryList');
    if (!accNoteCategories.length) {
        list.innerHTML = '<span class="acc2-empty">No note categories defined yet.</span>';
        return;
    }
    list.innerHTML = accNoteCategories.map(function (category, index) {
        const status = category.is_active ? 'Active' : 'Inactive';
        const action = category.is_active ? 'Deactivate' : 'Reactivate';
        const upDisabled = index === 0 ? ' disabled' : '';
        const downDisabled = index === accNoteCategories.length - 1 ? ' disabled' : '';
        return '<div class="acc2-category-item"><div><strong>' + accEscape(category.label) + '</strong><span>' + accEscape(category.category_key) + ' - ' + status + '</span></div><div class="acc2-category-actions"><button type="button" class="acc2-mini-action" data-acc-note-id="' + category.id + '" data-acc-note-action="rename">Rename</button><button type="button" class="acc2-mini-action" data-acc-note-id="' + category.id + '" data-acc-note-action="up"' + upDisabled + ' aria-label="Move ' + accEscape(category.label) + ' up">↑</button><button type="button" class="acc2-mini-action" data-acc-note-id="' + category.id + '" data-acc-note-action="down"' + downDisabled + ' aria-label="Move ' + accEscape(category.label) + ' down">↓</button><button type="button" class="acc2-mini-action" data-acc-note-id="' + category.id + '" data-acc-note-action="toggle" data-acc-note-active="' + (category.is_active ? 'true' : 'false') + '">' + action + '</button></div></div>';
    }).join('');
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
                if (childCode === accountCode || childCode.indexOf(accountCode + '-') === 0) visibleCodes.add(childCode);
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
        await accApi('/api/account-control-center/cashflow/treasury-accounts', { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ client_id: accClientId, cash_account_codes: accCashflowTreasury.cash || [], bank_account_codes: accCashflowTreasury.bank || [] }) });
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
    }
}
async function accLoadTrialBalance(forceRefresh) {
    const clientId = String(accClientId || '');
    if (!forceRefresh && accTrialBalanceCache.clientId === clientId && accTrialBalanceCache.accounts) {
        accAccounts = accTrialBalanceCache.accounts;
        accLatestUpload = accTrialBalanceCache.upload;
        return;
    }
    if (accTrialBalanceCache.request && accTrialBalanceCache.clientId === clientId) return accTrialBalanceCache.request;
    accTrialBalanceCache.clientId = clientId;
    const request = accApi('/api/account-control-center/mapping?client_id=' + encodeURIComponent(accClientId)).then(function (result) {
        accTrialBalanceCache.accounts = result.accounts || [];
        accTrialBalanceCache.upload = result.upload || null;
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
    const result = await accApi('/api/account-control-center/cashflow/setup?client_id=' + encodeURIComponent(accClientId));
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
    const result = await accApi('/api/account-control-center/categories?client_id=' + encodeURIComponent(accClientId) + '&category_type=financial_statement&include_inactive=true');
    accFinancialCategories = result.categories || [];
    accFinancialLoaded = true;
    accRenderFinancialCategoryList();
    accRenderFinancialRows();
}
async function accLoadNoteCategories() {
    const result = await accApi('/api/account-control-center/categories?client_id=' + encodeURIComponent(accClientId) + '&category_type=note&include_inactive=true');
    accNoteCategories = result.categories || [];
    accNotesLoaded = true;
    accRenderNoteCategoryList();
    accRenderNotesRows();
}
function accMappingPayload(accountCode, workstream) {
    return { client_id: accClientId, account_code: accountCode, verification_workstream: workstream || null };
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
            await accApi('/api/account-control-center/mapping/apply-to-children', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ client_id: accClientId, parent_account_code: account.account_code, verification_workstream: select.value || null }) });
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
            return accApi('/api/account-control-center/cashflow/category-assignment', { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ client_id: accClientId, account_code: account.account_code, category_id: categoryId }) });
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
            await accApi('/api/account-control-center/cashflow/category-assignment/apply-to-children', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ client_id: accClientId, parent_account_code: account.account_code, category_id: categoryId }) });
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
        await accWithBusy('Saving Financial Statement category...', function () {
            return accApi('/api/account-control-center/mapping', { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ client_id: accClientId, account_code: account.account_code, financial_statement_category_id: value }) });
        });
        accAssignment(account).financial_statement_category_id = value;
        saved = true;
        accShowStatus('Saved Financial Statement category for ' + account.account_code + '.', 'success');
    } finally {
        accSetInlineResult(select, result, saved);
    }
}
async function accApplyFinancialParent(account, select) {
    if (!window.confirm('Apply this Financial Statement category to all leaf accounts below ' + account.account_code + '?')) { accRenderFinancialRows(); return; }
    const value = select.value ? Number(select.value) : null;
    accSetSelectBusy(select, true);
    try {
        await accWithTableBusy('accFinancialGridWrap', 'Applying Financial Statement category to child accounts...', async function () {
            await accApi('/api/account-control-center/mapping/apply-to-children', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ client_id: accClientId, parent_account_code: account.account_code, financial_statement_category_id: value }) });
            await accLoadMapping(true);
        });
        accShowStatus('Applied the Financial Statement category to child accounts below ' + account.account_code + '.', 'success');
    } finally {
        accSetSelectBusy(select, false);
    }
}
async function accHandleFinancialChange(event) {
    const select = event.target.closest('.acc2-financial-category');
    if (!select) return;
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
async function accCreateFinancialCategory(event) {
    event.preventDefault();
    const key = accElement('accFinancialCategoryKey').value.trim();
    const label = accElement('accFinancialCategoryLabel').value.trim();
    const button = event.submitter || accElement('accFinancialCategoryForm').querySelector('button[type="submit"]');
    try {
        await accWithButtonBusy(button, 'Adding category...', async function () {
            await accApi('/api/account-control-center/categories', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ client_id: accClientId, category_type: 'financial_statement', category_key: key, label: label, sort_order: accFinancialCategories.length }) });
            accElement('accFinancialCategoryForm').reset();
            await accLoadFinancialCategories();
            accShowStatus('Financial Statement category added.', 'success');
        });
    } catch (error) { accShowStatus(error.message, 'error'); }
}
async function accUpdateFinancialCategory(category, patch, message) {
    await accApi('/api/account-control-center/categories/' + category.id, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ client_id: accClientId, category_type: 'financial_statement', label: patch.label == null ? category.label : patch.label, sort_order: patch.sort_order == null ? category.sort_order : patch.sort_order, is_active: patch.is_active == null ? category.is_active : patch.is_active }) });
    await accLoadFinancialCategories();
    accShowStatus(message, 'success');
}
async function accHandleFinancialCategoryAction(event) {
    const button = event.target.closest('[data-acc-category-id]');
    if (!button || button.disabled) return;
    const category = accFinancialCategories.find(function (item) { return String(item.id) === button.dataset.accCategoryId; });
    if (!category) return;
    const action = button.dataset.accCategoryAction;
    try {
        if (action === 'toggle') {
            await accWithButtonBusy(button, 'Updating category...', function () { return accUpdateFinancialCategory(category, { is_active: button.dataset.accCategoryActive !== 'true' }, 'Financial Statement category status updated.'); });
            return;
        }
        if (action === 'rename') {
            const label = window.prompt('Display label for ' + category.category_key, category.label);
            if (label == null || !label.trim() || label.trim() === category.label) return;
            await accWithButtonBusy(button, 'Renaming category...', function () { return accUpdateFinancialCategory(category, { label: label.trim() }, 'Financial Statement category renamed.'); });
            return;
        }
        const index = accFinancialCategories.findIndex(function (item) { return item.id === category.id; });
        const targetIndex = action === 'up' ? index - 1 : index + 1;
        const target = accFinancialCategories[targetIndex];
        if (!target) return;
        const updateCategoryOrder = function (item, sortOrder) {
            return accApi('/api/account-control-center/categories/' + item.id, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ client_id: accClientId, category_type: 'financial_statement', label: item.label, sort_order: sortOrder, is_active: item.is_active }) });
        };
        await accWithButtonBusy(button, 'Reordering categories...', async function () {
            await Promise.all([
                updateCategoryOrder(category, target.sort_order),
                updateCategoryOrder(target, category.sort_order),
            ]);
            await accLoadFinancialCategories();
            accShowStatus('Financial Statement category order updated.', 'success');
        });
    } catch (error) { accShowStatus(error.message, 'error'); }
}
async function accSaveNoteLeaf(account, select) {
    const result = accElement('accNotesRows').querySelector('[data-acc-note-result="' + CSS.escape(account.account_code) + '"]');
    const value = select.value ? Number(select.value) : null;
    accSetInlineBusy(select, result, 'Saving...');
    let saved = false;
    try {
        await accWithBusy('Saving note category...', function () {
            return accApi('/api/account-control-center/mapping', { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ client_id: accClientId, account_code: account.account_code, note_category_id: value }) });
        });
        accAssignment(account).note_category_id = value;
        saved = true;
        accShowStatus('Saved note category for ' + account.account_code + '.', 'success');
    } finally {
        accSetInlineResult(select, result, saved);
    }
}
async function accApplyNoteParent(account, select) {
    if (!window.confirm('Apply this note category to all leaf accounts below ' + account.account_code + '?')) { accRenderNotesRows(); return; }
    const value = select.value ? Number(select.value) : null;
    accSetSelectBusy(select, true);
    try {
        await accWithTableBusy('accNotesGridWrap', 'Applying note category to child accounts...', async function () {
            await accApi('/api/account-control-center/mapping/apply-to-children', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ client_id: accClientId, parent_account_code: account.account_code, note_category_id: value }) });
            await accLoadMapping(true);
        });
        accShowStatus('Applied the note category to child accounts below ' + account.account_code + '.', 'success');
    } finally {
        accSetSelectBusy(select, false);
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
async function accCreateNoteCategory(event) {
    event.preventDefault();
    const key = accElement('accNotesCategoryKey').value.trim();
    const label = accElement('accNotesCategoryLabel').value.trim();
    const button = event.submitter || accElement('accNotesCategoryForm').querySelector('button[type="submit"]');
    try {
        await accWithButtonBusy(button, 'Adding category...', async function () {
            await accApi('/api/account-control-center/categories', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ client_id: accClientId, category_type: 'note', category_key: key, label: label, sort_order: accNoteCategories.length }) });
            accElement('accNotesCategoryForm').reset();
            await accLoadNoteCategories();
            accShowStatus('Note category added.', 'success');
        });
    } catch (error) { accShowStatus(error.message, 'error'); }
}
async function accUpdateNoteCategory(category, patch, message) {
    await accApi('/api/account-control-center/categories/' + category.id, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ client_id: accClientId, category_type: 'note', label: patch.label == null ? category.label : patch.label, sort_order: patch.sort_order == null ? category.sort_order : patch.sort_order, is_active: patch.is_active == null ? category.is_active : patch.is_active }) });
    await accLoadNoteCategories();
    accShowStatus(message, 'success');
}
async function accHandleNoteCategoryAction(event) {
    const button = event.target.closest('[data-acc-note-id]');
    if (!button || button.disabled) return;
    const category = accNoteCategories.find(function (item) { return String(item.id) === button.dataset.accNoteId; });
    if (!category) return;
    const action = button.dataset.accNoteAction;
    try {
        if (action === 'toggle') {
            await accWithButtonBusy(button, 'Updating category...', function () { return accUpdateNoteCategory(category, { is_active: button.dataset.accNoteActive !== 'true' }, 'Note category status updated.'); });
            return;
        }
        if (action === 'rename') {
            const label = window.prompt('Display label for ' + category.category_key, category.label);
            if (label == null || !label.trim() || label.trim() === category.label) return;
            await accWithButtonBusy(button, 'Renaming category...', function () { return accUpdateNoteCategory(category, { label: label.trim() }, 'Note category renamed.'); });
            return;
        }
        const index = accNoteCategories.findIndex(function (item) { return item.id === category.id; });
        const target = accNoteCategories[action === 'up' ? index - 1 : index + 1];
        if (!target) return;
        const updateCategoryOrder = function (item, sortOrder) {
            return accApi('/api/account-control-center/categories/' + item.id, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ client_id: accClientId, category_type: 'note', label: item.label, sort_order: sortOrder, is_active: item.is_active }) });
        };
        await accWithButtonBusy(button, 'Reordering categories...', async function () {
            await Promise.all([updateCategoryOrder(category, target.sort_order), updateCategoryOrder(target, category.sort_order)]);
            await accLoadNoteCategories();
            accShowStatus('Note category order updated.', 'success');
        });
    } catch (error) { accShowStatus(error.message, 'error'); }
}
function accSetCashflowConfigTab(tabName) {
    document.querySelectorAll('[data-acc-cashflow-config-tab]').forEach(function (tab) { tab.classList.toggle('active', tab.dataset.accCashflowConfigTab === tabName); });
    document.querySelectorAll('[data-acc-cashflow-config-panel]').forEach(function (panel) { panel.hidden = panel.dataset.accCashflowConfigPanel !== tabName; });
}
function accSetCashflowModal(id, open) { const modal = accElement(id); if (modal) modal.hidden = !open; }
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
    accElement('accCashflowCategoryFab').hidden = tabName !== 'cashflow';
    if (tabName === 'cashflow' && !accCashflowLoaded) {
        accSetCashflowGridState('Loading Cash Flow Setup...', false, true);
        accLoadLazyTab('cashflow', 'Loading Cash Flow Setup...', accLoadCashflow, accSetCashflowGridState);
    }
    if (tabName === 'financial' && !accFinancialLoaded) {
        accSetFinancialGridState('Loading Financial Statement categories...', false, true);
        accLoadLazyTab('financial', 'Loading Financial Statement categories...', accLoadFinancialCategories, accSetFinancialGridState);
    }
    if (tabName === 'notes' && !accNotesLoaded) {
        accSetNotesGridState('Loading note categories...', false, true);
        accLoadLazyTab('notes', 'Loading note categories...', accLoadNoteCategories, accSetNotesGridState);
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
    accEnsureCashflowComboboxControls();
    document.addEventListener('input', accHandleCashflowComboboxInput);
    document.querySelectorAll('[data-acc-tab]').forEach(function (tab) { tab.addEventListener('click', function () { accSetTab(tab.dataset.accTab); }); });
    accElement('accAccountFilter').addEventListener('input', accRenderRows);
    accElement('accAccountRows').addEventListener('change', function (event) { accHandleWorkstreamChange(event); });
    accElement('accCashflowFilter').addEventListener('input', accRenderCashflowRows);
    accElement('accCashflowRows').addEventListener('change', function (event) { accHandleCashflowChange(event); });
    accElement('accCashflowCategoryFab').addEventListener('click', function () { accSetCashflowModal('accCashflowCategoryModal', true); accElement('accCashflowCategoryLabel').focus(); });
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
    document.addEventListener('keydown', function (event) { if (event.key === 'Escape') { accSetCashflowModal('accCashflowCategoryModal', false); accSetCashflowModal('accCashflowAccountsModal', false); accSetCashflowModal('accCashflowTemplateModal', false); } });
    accElement('accFinancialFilter').addEventListener('input', accRenderFinancialRows);
    accElement('accFinancialRows').addEventListener('change', function (event) { accHandleFinancialChange(event); });
    accElement('accFinancialCategoryForm').addEventListener('submit', accCreateFinancialCategory);
    accElement('accFinancialCategoryList').addEventListener('click', function (event) { accHandleFinancialCategoryAction(event); });
    accElement('accNotesFilter').addEventListener('input', accRenderNotesRows);
    accElement('accNotesRows').addEventListener('change', function (event) { accHandleNoteChange(event); });
    accElement('accNotesCategoryForm').addEventListener('submit', accCreateNoteCategory);
    accElement('accNotesCategoryList').addEventListener('click', function (event) { accHandleNoteCategoryAction(event); });
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
