// Simplified Account Control Centre: Verification and Financial Statement mappings.
let accClientId = null;
let accAccounts = [];
let accLatestUpload = null;
let accFinancialCategories = [];
let accFinancialLoaded = false;
let accIsAdmin = false;

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
function accOptionMarkup(selected) {
    let html = '<option value="">Unassigned</option>';
    if (selected === '__mixed') html = '<option value="__mixed" selected>Mixed assignments - choose one</option>' + html;
    html += ACC_WORKSTREAMS.map(function (item) {
        return '<option value="' + item.value + '"' + (item.value === selected ? ' selected' : '') + '>' + item.label + '</option>';
    }).join('');
    return html;
}
function accCategoryOptionMarkup(selected) {
    let html = '<option value="">Unassigned</option>';
    if (selected === '__mixed') html = '<option value="__mixed" selected>Mixed assignments - choose one</option>' + html;
    html += accFinancialCategories.map(function (category) {
        const value = String(category.id);
        const inactive = !category.is_active;
        const label = (inactive ? 'Inactive: ' : '') + category.label;
        return '<option value="' + accEscape(value) + '"' + (value === String(selected) ? ' selected' : '') + (inactive && value !== String(selected) ? ' disabled' : '') + '>' + accEscape(label) + '</option>';
    }).join('');
    return html;
}
function accSetGridState(message, isError) {
    const state = accElement('accGridState');
    state.textContent = message;
    state.className = 'acc2-grid-state' + (isError ? ' error' : '');
    state.hidden = false;
    accElement('accGridWrap').hidden = true;
}
function accSetFinancialGridState(message, isError) {
    const state = accElement('accFinancialGridState');
    state.textContent = message;
    state.className = 'acc2-grid-state' + (isError ? ' error' : '');
    state.hidden = false;
    accElement('accFinancialGridWrap').hidden = true;
}
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
    accElement('accGridState').hidden = true;
    accElement('accGridWrap').hidden = false;
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
    accElement('accFinancialGridState').hidden = true;
    accElement('accFinancialGridWrap').hidden = false;
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
async function accLoadMapping() {
    const result = await accApi('/api/account-control-center/mapping?client_id=' + encodeURIComponent(accClientId));
    accAccounts = result.accounts || [];
    accLatestUpload = result.upload || null;
    accRenderSource();
    accRenderRows();
    if (accFinancialLoaded) accRenderFinancialRows();
}
async function accLoadFinancialCategories() {
    const result = await accApi('/api/account-control-center/categories?client_id=' + encodeURIComponent(accClientId) + '&category_type=financial_statement&include_inactive=true');
    accFinancialCategories = result.categories || [];
    accFinancialLoaded = true;
    accRenderFinancialCategoryList();
    accRenderFinancialRows();
}
function accMappingPayload(accountCode, workstream) {
    return { client_id: accClientId, account_code: accountCode, verification_workstream: workstream || null };
}
async function accSaveLeaf(account, select) {
    const result = accElement('accAccountRows').querySelector('[data-acc-result="' + CSS.escape(account.account_code) + '"]');
    if (result) result.textContent = 'Saving...';
    await accApi('/api/account-control-center/mapping', { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(accMappingPayload(account.account_code, select.value)) });
    accAssignment(account).verification_workstream = select.value || null;
    if (result) result.textContent = 'Saved';
    accShowStatus('Saved ' + account.account_code + '.', 'success');
}
async function accApplyParent(account, select) {
    if (!window.confirm('Apply this verification sub-module to all leaf accounts below ' + account.account_code + '?')) { accRenderRows(); return; }
    await accApi('/api/account-control-center/mapping/apply-to-children', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ client_id: accClientId, parent_account_code: account.account_code, verification_workstream: select.value || null }) });
    await accLoadMapping();
    accShowStatus('Applied the assignment to the child accounts below ' + account.account_code + '.', 'success');
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
        await accLoadMapping().catch(function () {});
    }
}
async function accSaveFinancialLeaf(account, select) {
    const result = accElement('accFinancialRows').querySelector('[data-acc-fs-result="' + CSS.escape(account.account_code) + '"]');
    if (result) result.textContent = 'Saving...';
    const value = select.value ? Number(select.value) : null;
    await accApi('/api/account-control-center/mapping', { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ client_id: accClientId, account_code: account.account_code, financial_statement_category_id: value }) });
    accAssignment(account).financial_statement_category_id = value;
    if (result) result.textContent = 'Saved';
    accShowStatus('Saved Financial Statement category for ' + account.account_code + '.', 'success');
}
async function accApplyFinancialParent(account, select) {
    if (!window.confirm('Apply this Financial Statement category to all leaf accounts below ' + account.account_code + '?')) { accRenderFinancialRows(); return; }
    const value = select.value ? Number(select.value) : null;
    await accApi('/api/account-control-center/mapping/apply-to-children', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ client_id: accClientId, parent_account_code: account.account_code, financial_statement_category_id: value }) });
    await accLoadMapping();
    accShowStatus('Applied the Financial Statement category to child accounts below ' + account.account_code + '.', 'success');
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
        await accLoadMapping().catch(function () {});
    }
}
async function accCreateFinancialCategory(event) {
    event.preventDefault();
    const key = accElement('accFinancialCategoryKey').value.trim();
    const label = accElement('accFinancialCategoryLabel').value.trim();
    try {
        await accApi('/api/account-control-center/categories', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ client_id: accClientId, category_type: 'financial_statement', category_key: key, label: label, sort_order: accFinancialCategories.length }) });
        accElement('accFinancialCategoryForm').reset();
        await accLoadFinancialCategories();
        accShowStatus('Financial Statement category added.', 'success');
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
            await accUpdateFinancialCategory(category, { is_active: button.dataset.accCategoryActive !== 'true' }, 'Financial Statement category status updated.');
            return;
        }
        if (action === 'rename') {
            const label = window.prompt('Display label for ' + category.category_key, category.label);
            if (label == null || !label.trim() || label.trim() === category.label) return;
            await accUpdateFinancialCategory(category, { label: label.trim() }, 'Financial Statement category renamed.');
            return;
        }
        const index = accFinancialCategories.findIndex(function (item) { return item.id === category.id; });
        const targetIndex = action === 'up' ? index - 1 : index + 1;
        const target = accFinancialCategories[targetIndex];
        if (!target) return;
        const updateCategoryOrder = function (item, sortOrder) {
            return accApi('/api/account-control-center/categories/' + item.id, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ client_id: accClientId, category_type: 'financial_statement', label: item.label, sort_order: sortOrder, is_active: item.is_active }) });
        };
        await Promise.all([
            updateCategoryOrder(category, target.sort_order),
            updateCategoryOrder(target, category.sort_order),
        ]);
        await accLoadFinancialCategories();
        accShowStatus('Financial Statement category order updated.', 'success');
    } catch (error) { accShowStatus(error.message, 'error'); }
}
function accSetTab(tabName) {
    document.querySelectorAll('[data-acc-tab]').forEach(function (tab) {
        const active = tab.dataset.accTab === tabName;
        tab.classList.toggle('active', active);
        tab.setAttribute('aria-selected', active ? 'true' : 'false');
    });
    accElement('accVerificationPanel').hidden = tabName !== 'verification';
    accElement('accFinancialPanel').hidden = tabName !== 'financial';
    accElement('accNotesPanel').hidden = tabName !== 'notes';
    if (tabName === 'financial' && !accFinancialLoaded) accLoadFinancialCategories().catch(function (error) { accSetFinancialGridState(error.message, true); });
}
function accBindEvents() {
    document.querySelectorAll('[data-acc-tab]').forEach(function (tab) { tab.addEventListener('click', function () { accSetTab(tab.dataset.accTab); }); });
    accElement('accAccountFilter').addEventListener('input', accRenderRows);
    accElement('accAccountRows').addEventListener('change', function (event) { accHandleWorkstreamChange(event); });
    accElement('accFinancialFilter').addEventListener('input', accRenderFinancialRows);
    accElement('accFinancialRows').addEventListener('change', function (event) { accHandleFinancialChange(event); });
    accElement('accFinancialCategoryForm').addEventListener('submit', accCreateFinancialCategory);
    accElement('accFinancialCategoryList').addEventListener('click', function (event) { accHandleFinancialCategoryAction(event); });
}
async function initAccountControlCentre() {
    if (window.clientContextReady) await window.clientContextReady;
    accBindEvents();
    accClientId = window.getSelectedClientId ? window.getSelectedClientId() : null;
    if (!accClientId) { accSetGridState('Select a client before opening Account Control Centre.', true); return; }
    accIsAdmin = await window.isAdminUser();
    if (!accIsAdmin) accElement('accAdminWarning').hidden = false;
    try { await accLoadMapping(); } catch (error) { accSetGridState(error.message, true); }
}
document.addEventListener('DOMContentLoaded', initAccountControlCentre);
