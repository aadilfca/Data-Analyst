// Simplified Account Control Centre: one Trial Balance grid with independent future mappings.
let accClientId = null;
let accAccounts = [];
let accLatestUpload = null;
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
function accOptionMarkup(selected, disabled) {
    let html = '<option value="">Unassigned</option>';
    if (selected === '__mixed') html = '<option value="__mixed" selected>Mixed assignments — choose one</option>' + html;
    html += ACC_WORKSTREAMS.map(function (item) {
        return '<option value="' + item.value + '"' + (item.value === selected ? ' selected' : '') + '>' + item.label + '</option>';
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
                '<td><select class="acc2-workstream" data-acc-code="' + accEscape(account.account_code) + '" data-acc-parent="' + (isParent ? 'true' : 'false') + '"' + (!accIsAdmin ? ' disabled' : '') + '>' + accOptionMarkup(selected, !accIsAdmin) + '</select></td>' +
                '<td>' + result + '</td>' +
                '</tr>';
        }).join('');
    }
    accElement('accGridState').hidden = true;
    accElement('accGridWrap').hidden = false;
}
function accRenderSource() {
    const upload = accLatestUpload;
    if (!upload) {
        accElement('accSourceInfo').textContent = 'No parsed Trial Balance';
        return;
    }
    accElement('accSourceInfo').textContent = 'Source: ' + (upload.file_name || 'Latest Trial Balance') + (upload.as_of_date ? ' · ' + upload.as_of_date : '');
}
async function accLoadMapping() {
    const result = await accApi('/api/account-control-center/mapping?client_id=' + encodeURIComponent(accClientId));
    accAccounts = result.accounts || [];
    accLatestUpload = result.upload || null;
    accRenderSource();
    accRenderRows();
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
    if (!window.confirm('Apply this verification sub-module to all leaf accounts below ' + account.account_code + '?')) {
        accRenderRows();
        return;
    }
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
function accSetTab(tabName) {
    document.querySelectorAll('[data-acc-tab]').forEach(function (tab) {
        const active = tab.dataset.accTab === tabName;
        tab.classList.toggle('active', active);
        tab.setAttribute('aria-selected', active ? 'true' : 'false');
    });
    accElement('accVerificationPanel').hidden = tabName !== 'verification';
    accElement('accFinancialPanel').hidden = tabName !== 'financial';
    accElement('accNotesPanel').hidden = tabName !== 'notes';
}
function accBindEvents() {
    document.querySelectorAll('[data-acc-tab]').forEach(function (tab) {
        tab.addEventListener('click', function () { accSetTab(tab.dataset.accTab); });
    });
    accElement('accAccountFilter').addEventListener('input', accRenderRows);
    accElement('accAccountRows').addEventListener('change', function (event) { accHandleWorkstreamChange(event); });
}
async function initAccountControlCentre() {
    if (window.clientContextReady) await window.clientContextReady;
    accBindEvents();
    accClientId = window.getSelectedClientId ? window.getSelectedClientId() : null;
    if (!accClientId) {
        accSetGridState('Select a client before opening Account Control Centre.', true);
        return;
    }
    accIsAdmin = await window.isAdminUser();
    if (!accIsAdmin) accElement('accAdminWarning').hidden = false;
    try {
        await accLoadMapping();
    } catch (error) {
        accSetGridState(error.message, true);
    }
}
document.addEventListener('DOMContentLoaded', initAccountControlCentre);
