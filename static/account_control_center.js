// Simplified Account Control Centre: Verification and Financial Statement mappings.
let accClientId = null;
let accAccounts = [];
let accLatestUpload = null;
let accFinancialCategories = [];
let accNoteCategories = [];
let accCashflowCategories = [];
let accCashflowHistoricalOrphans = [];
let accCashflowTreasury = { cash: [], bank: [] };
let accCashflowSettings = { match_internal_transfers: false, receipt_presentation: 'net', source_template_id: null };
let accCashflowTemplateMapping = null;
let accFinancialLoaded = false;
let accNotesLoaded = false;
let accCashflowLoaded = false;
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
function accSetCashflowGridState(message, isError) {
    const state = accElement('accCashflowGridState');
    state.textContent = message;
    state.className = 'acc2-grid-state' + (isError ? ' error' : '');
    state.hidden = false;
    accElement('accCashflowGridWrap').hidden = true;
}
function accEffectiveCashflowCategory(account) {
    const direct = account.category_id;
    if (account.is_leaf || direct != null) return direct == null ? '' : String(direct);
    const values = Array.from(new Set(accLeavesBelow(account.account_code).map(function (leaf) {
        return leaf.category_id == null ? '' : String(leaf.category_id);
    })));
    return values.length === 1 ? values[0] : (values.length > 1 ? '__mixed' : '');
}
function accRenderCashflowRows() {
    const filter = String(accElement('accCashflowFilter').value || '').trim().toLowerCase();
    const visible = accAccounts.filter(function (account) {
        return !filter || (String(account.account_code || '') + ' ' + String(account.account_name || '')).toLowerCase().indexOf(filter) !== -1;
    });
    const visibleOrphans = accCashflowHistoricalOrphans.filter(function (orphan) {
        return !filter || (String(orphan.account_code || '') + ' ' + String(orphan.account_name || '')).toLowerCase().indexOf(filter) !== -1;
    });
    accElement('accCashflowCount').textContent = (visible.length + visibleOrphans.length) + ' of ' + (accAccounts.length + accCashflowHistoricalOrphans.length) + ' accounts';
    accElement('accCashflowOrphanNotice').hidden = !accCashflowHistoricalOrphans.length;
    const rows = accElement('accCashflowRows');
    function orphanMarkup(orphan) {
        const parent = orphan.parent_account_code ? 'Under ' + orphan.parent_account_code + (orphan.parent_account_name ? ' — ' + orphan.parent_account_name : '') : 'No surviving parent found';
        const detail = (orphan.transaction_count || 0) + ' transaction(s), ' + (orphan.voucher_count || 0) + ' voucher(s)';
        const selected = orphan.category_id == null ? '' : String(orphan.category_id);
        const result = '<span class="acc2-saved" data-acc-cashflow-result="' + accEscape(orphan.account_code) + '">Saved</span>';
        return '<tr class="acc2-historical-orphan" data-acc-historical-orphan="' + accEscape(orphan.account_code) + '">' +
            '<td class="acc2-code">' + accEscape(orphan.account_code) + '</td>' +
            '<td><strong>' + accEscape(orphan.account_name || 'Historical account') + '</strong><small class="acc2-orphan-parent">' + accEscape(parent) + '</small></td>' +
            '<td><span class="acc2-type historical">Historical / orphan</span></td>' +
            '<td><select class="acc2-cashflow-category" data-acc-code="' + accEscape(orphan.account_code) + '" data-acc-parent="false" data-acc-historical-orphan="true"' + (!accIsAdmin ? ' disabled' : '') + '>' + accCashflowCategoryOptionMarkup(selected) + '</select></td>' +
            '<td>' + result + '<small class="acc2-orphan-detail">' + accEscape(detail) + '</small></td>' +
            '</tr>';
    }
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
            (orphanByInsertIndex['0'] || []).forEach(function (orphan) { rendered.push(orphanMarkup(orphan)); });
        } else {
            visible.forEach(function (account, index) {
                const isParent = !account.is_leaf;
                const selected = isParent ? accEffectiveCashflowCategory(account) : (account.category_id == null ? '' : String(account.category_id));
                const indent = Math.max(0, Number(account.level || 1) - 1) * 16;
                const result = isParent ? '<span class="acc2-parent-help">Applies to children</span>' : '<span class="acc2-saved" data-acc-cashflow-result="' + accEscape(account.account_code) + '">Saved</span>';
                rendered.push('<tr class="' + (isParent ? 'acc2-parent' : '') + '">' +
                    '<td class="acc2-code" style="padding-left:' + (indent + 10) + 'px">' + accEscape(account.account_code) + '</td>' +
                    '<td>' + accEscape(account.account_name || 'Unknown account') + '</td>' +
                    '<td><span class="acc2-type ' + (isParent ? 'parent' : 'leaf') + '">' + (isParent ? 'Parent' : 'Leaf') + '</span></td>' +
                    '<td><select class="acc2-cashflow-category" data-acc-code="' + accEscape(account.account_code) + '" data-acc-parent="' + (isParent ? 'true' : 'false') + '"' + (!accIsAdmin ? ' disabled' : '') + '>' + accCashflowCategoryOptionMarkup(selected) + '</select></td>' +
                    '<td>' + result + '</td>' +
                    '</tr>');
                (orphanByInsertIndex[String(index + 1)] || []).forEach(function (orphan) { rendered.push(orphanMarkup(orphan)); });
            });
        }
        rows.innerHTML = rendered.join('');
    }
    accElement('accCashflowGridState').hidden = true;
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
    accElement('accFinancialGridState').hidden = true;
    accElement('accFinancialGridWrap').hidden = false;
}
function accSetNotesGridState(message, isError) {
    const state = accElement('accNotesGridState');
    state.textContent = message;
    state.className = 'acc2-grid-state' + (isError ? ' error' : '');
    state.hidden = false;
    accElement('accNotesGridWrap').hidden = true;
}
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
    accElement('accNotesGridState').hidden = true;
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
    try {
        await accApi('/api/account-control-center/cashflow/categories', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ client_id: accClientId, category_key: key, label: label, sort_order: accCashflowCategories.length }) });
        accElement('accCashflowCategoryForm').reset();
        await accLoadCashflow();
        accShowStatus('Cash Flow category added.', 'success');
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
        if (action === 'toggle') { await accUpdateCashflowCategory(category, { is_active: button.dataset.accCashflowCategoryActive !== 'true' }, 'Cash Flow category status updated.'); return; }
        if (action === 'rename') { const label = window.prompt('Display label for ' + category.category_key, category.label); if (label && label.trim() && label.trim() !== category.label) await accUpdateCashflowCategory(category, { label: label.trim() }, 'Cash Flow category renamed.'); return; }
        const index = accCashflowCategories.findIndex(function (item) { return item.id === category.id; });
        const target = accCashflowCategories[action === 'up' ? index - 1 : index + 1];
        if (!target) return;
        await Promise.all([accUpdateCashflowCategory(category, { sort_order: target.sort_order }, 'Cash Flow category order updated.'), accUpdateCashflowCategory(target, { sort_order: category.sort_order }, 'Cash Flow category order updated.')]);
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
async function accSaveCashflowTreasury() {
    await accApi('/api/account-control-center/cashflow/treasury-accounts', { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ client_id: accClientId, cash_account_codes: accCashflowTreasury.cash || [], bank_account_codes: accCashflowTreasury.bank || [] }) });
    accShowStatus('Cash and Bank account selections saved.', 'success');
}
async function accSaveCashflowSettings() {
    const payload = { client_id: accClientId, match_internal_transfers: accElement('accCashflowMatchTransfers').checked, receipt_presentation: accElement('accCashflowReceiptPresentation').value };
    await accApi('/api/account-control-center/cashflow/settings', { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) });
    accCashflowSettings = { match_internal_transfers: payload.match_internal_transfers, receipt_presentation: payload.receipt_presentation };
    accShowStatus('Cash Flow transfer and customer receipt settings saved.', 'success');
}
async function accLoadMapping() {
    const result = await accApi('/api/account-control-center/mapping?client_id=' + encodeURIComponent(accClientId));
    accAccounts = result.accounts || [];
    accLatestUpload = result.upload || null;
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
    const mapping = await accApi('/api/reconciliation/cashflows/template-columns?client_id=' + encodeURIComponent(accClientId) + '&template_id=' + encodeURIComponent(templateId));
    accRenderCashflowTemplateMapping(mapping);
}
async function accOpenCashflowTemplateModal() {
    accSetCashflowModal('accCashflowTemplateModal', true);
    accElement('accCashflowTemplateSelect').disabled = true;
    accElement('accCashflowTemplateSelect').innerHTML = '<option value="">Loading templates...</option>';
    accSetCashflowTemplateState('Loading Client Data Bank templates...');
    try {
        const result = await accApi('/api/reconciliation/cashflows/sources?client_id=' + encodeURIComponent(accClientId));
        const options = accRenderCashflowTemplateSources(result.sources || []);
        const selected = accElement('accCashflowTemplateSelect').value;
        if (selected && options.length) await accLoadCashflowTemplateMapping(selected);
    } catch (error) {
        accSetCashflowTemplateState(error.message, true);
    }
}
async function accSaveCashflowTemplate() {
    const templateId = Number(accElement('accCashflowTemplateSelect').value || 0);
    if (!templateId || !accCashflowTemplateMappingIsReady()) return;
    const mappings = Array.from(accElement('accCashflowTemplateMapping').querySelectorAll('[data-acc-template-role]')).filter(function (select) { return select.value; }).map(function (select) {
        return { role: select.dataset.accTemplateRole, template_column_id: Number(select.value) };
    });
    const result = await accApi('/api/reconciliation/cashflows/template-columns', { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ client_id: accClientId, template_id: templateId, mappings: mappings, set_as_default: true }) });
    accCashflowSettings.source_template_id = templateId;
    accRenderCashflowTemplateMapping(result);
    accShowStatus('Cashflows transaction template mapping saved.', 'success');
}
async function accLoadCashflow() {
    const result = await accApi('/api/account-control-center/cashflow/setup?client_id=' + encodeURIComponent(accClientId));
    accAccounts = result.accounts || [];
    accLatestUpload = result.upload || accLatestUpload;
    accCashflowCategories = result.categories || [];
    accCashflowHistoricalOrphans = result.historical_orphans || [];
    accCashflowTreasury = result.treasury_accounts || { cash: [], bank: [] };
    accCashflowSettings = result.settings || { match_internal_transfers: false, receipt_presentation: 'net', source_template_id: null };
    accCashflowLoaded = true;
    accRenderCashflowRows();
    accRenderCashflowCategoryList();
    accRenderCashflowAccountOptions();
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
async function accSaveCashflowCategory(account, select) {
    const categoryId = select.value && select.value !== '__mixed' ? Number(select.value) : null;
    const result = accElement('accCashflowRows').querySelector('[data-acc-cashflow-result="' + CSS.escape(account.account_code) + '"]');
    if (result) result.textContent = 'Saving...';
    await accApi('/api/account-control-center/cashflow/category-assignment', { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ client_id: accClientId, account_code: account.account_code, category_id: categoryId }) });
    account.category_id = categoryId;
    if (result) result.textContent = 'Saved';
    accShowStatus('Saved Cash Flow category for ' + account.account_code + '.', 'success');
}
async function accApplyCashflowCategoryParent(account, select) {
    if (!window.confirm('Apply this Cash Flow category to all leaf accounts below ' + account.account_code + '?')) { accRenderCashflowRows(); return; }
    const categoryId = select.value && select.value !== '__mixed' ? Number(select.value) : null;
    await accApi('/api/account-control-center/cashflow/category-assignment/apply-to-children', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ client_id: accClientId, parent_account_code: account.account_code, category_id: categoryId }) });
    await accLoadCashflow();
    accShowStatus('Applied the Cash Flow category to child accounts below ' + account.account_code + '.', 'success');
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
async function accSaveNoteLeaf(account, select) {
    const result = accElement('accNotesRows').querySelector('[data-acc-note-result="' + CSS.escape(account.account_code) + '"]');
    if (result) result.textContent = 'Saving...';
    const value = select.value ? Number(select.value) : null;
    await accApi('/api/account-control-center/mapping', { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ client_id: accClientId, account_code: account.account_code, note_category_id: value }) });
    accAssignment(account).note_category_id = value;
    if (result) result.textContent = 'Saved';
    accShowStatus('Saved note category for ' + account.account_code + '.', 'success');
}
async function accApplyNoteParent(account, select) {
    if (!window.confirm('Apply this note category to all leaf accounts below ' + account.account_code + '?')) { accRenderNotesRows(); return; }
    const value = select.value ? Number(select.value) : null;
    await accApi('/api/account-control-center/mapping/apply-to-children', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ client_id: accClientId, parent_account_code: account.account_code, note_category_id: value }) });
    await accLoadMapping();
    accShowStatus('Applied the note category to child accounts below ' + account.account_code + '.', 'success');
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
        await accLoadMapping().catch(function () {});
    }
}
async function accCreateNoteCategory(event) {
    event.preventDefault();
    const key = accElement('accNotesCategoryKey').value.trim();
    const label = accElement('accNotesCategoryLabel').value.trim();
    try {
        await accApi('/api/account-control-center/categories', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ client_id: accClientId, category_type: 'note', category_key: key, label: label, sort_order: accNoteCategories.length }) });
        accElement('accNotesCategoryForm').reset();
        await accLoadNoteCategories();
        accShowStatus('Note category added.', 'success');
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
            await accUpdateNoteCategory(category, { is_active: button.dataset.accNoteActive !== 'true' }, 'Note category status updated.');
            return;
        }
        if (action === 'rename') {
            const label = window.prompt('Display label for ' + category.category_key, category.label);
            if (label == null || !label.trim() || label.trim() === category.label) return;
            await accUpdateNoteCategory(category, { label: label.trim() }, 'Note category renamed.');
            return;
        }
        const index = accNoteCategories.findIndex(function (item) { return item.id === category.id; });
        const target = accNoteCategories[action === 'up' ? index - 1 : index + 1];
        if (!target) return;
        const updateCategoryOrder = function (item, sortOrder) {
            return accApi('/api/account-control-center/categories/' + item.id, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ client_id: accClientId, category_type: 'note', label: item.label, sort_order: sortOrder, is_active: item.is_active }) });
        };
        await Promise.all([updateCategoryOrder(category, target.sort_order), updateCategoryOrder(target, category.sort_order)]);
        await accLoadNoteCategories();
        accShowStatus('Note category order updated.', 'success');
    } catch (error) { accShowStatus(error.message, 'error'); }
}
function accSetCashflowConfigTab(tabName) {
    document.querySelectorAll('[data-acc-cashflow-config-tab]').forEach(function (tab) { tab.classList.toggle('active', tab.dataset.accCashflowConfigTab === tabName); });
    document.querySelectorAll('[data-acc-cashflow-config-panel]').forEach(function (panel) { panel.hidden = panel.dataset.accCashflowConfigPanel !== tabName; });
}
function accSetCashflowModal(id, open) { const modal = accElement(id); if (modal) modal.hidden = !open; }
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
    if (tabName === 'cashflow' && !accCashflowLoaded) accLoadCashflow().catch(function (error) { accSetCashflowGridState(error.message, true); });
    if (tabName === 'financial' && !accFinancialLoaded) accLoadFinancialCategories().catch(function (error) { accSetFinancialGridState(error.message, true); });
    if (tabName === 'notes' && !accNotesLoaded) accLoadNoteCategories().catch(function (error) { accSetNotesGridState(error.message, true); });
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
    accElement('accCashflowAccountsSave').addEventListener('click', function () { accSaveCashflowTreasury().catch(function (error) { accShowStatus(error.message, 'error'); }); });
    accElement('accCashflowSettingsSave').addEventListener('click', function () { accSaveCashflowSettings().catch(function (error) { accShowStatus(error.message, 'error'); }); });
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
    if (window.clientContextReady) await window.clientContextReady;
    accBindEvents();
    accClientId = window.getSelectedClientId ? window.getSelectedClientId() : null;
    if (!accClientId) { accSetGridState('Select a client before opening Account Control Centre.', true); return; }
    accIsAdmin = await window.isAdminUser();
    if (!accIsAdmin) accElement('accAdminWarning').hidden = false;
    try { await accLoadMapping(); } catch (error) { accSetGridState(error.message, true); }
}
document.addEventListener('DOMContentLoaded', initAccountControlCentre);
