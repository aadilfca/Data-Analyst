/* Shared Cashflow category setup UI for Trial Balance and Account Control Centre. */
(function () {
    'use strict';

    const escapeHtml = value => String(value == null ? '' : value).replace(/[&<>\'"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[c]));
    const ROLES = [
        ['customer_receipt', 'Customer receipts'], ['bank_charge', 'Bank charges'],
        ['withholding_tax', 'Withholding tax'], ['other_deduction', 'Other deductions'],
    ];

    function create(host) {
        let setup = null;
        let state = null;
        let currentTab = 'categories';
        const el = id => document.getElementById(id);
        const clientId = () => host.clientId;
        const uploadId = () => host.uploadId || null;
        const json = async (url, options) => {
            const response = await host.request(url, options);
            const body = await response.json().catch(() => ({}));
            if (!response.ok) throw new Error(body.detail || body.message || 'Cashflow setup could not be saved.');
            return body;
        };
        const query = () => {
            const params = new URLSearchParams({client_id: String(clientId())});
            if (uploadId()) params.set('trial_balance_upload_id', String(uploadId()));
            return params.toString();
        };
        function ensure() {
            if (setup) return;
            document.head.insertAdjacentHTML('beforeend', '<style id="cashflowWorkspaceStyles">' + (host.styles || '') + '</style>');
            document.body.insertAdjacentHTML('beforeend', '<div class="cfw-backdrop" id="cfwBackdrop" hidden><section class="cfw-modal" role="dialog" aria-modal="true" aria-labelledby="cfwTitle"><header class="cfw-head"><div><h2 id="cfwTitle">Cashflow setup</h2><p>Manage assignments and report settings for this client.</p></div><button type="button" class="cfw-close" data-cfw-close aria-label="Close">×</button></header><nav class="cfw-tabs" role="tablist"><button type="button" role="tab" data-cfw-tab="categories">Categories</button><button type="button" role="tab" data-cfw-tab="accounts">Cash &amp; Bank</button><button type="button" role="tab" data-cfw-tab="settings">Transfers &amp; receipts</button><button type="button" role="tab" data-cfw-tab="template">Transaction template</button></nav><div class="cfw-status" id="cfwStatus" role="status" hidden></div><section class="cfw-panel" data-cfw-panel="categories"><form id="cfwCategoryForm" class="cfw-create"><label>Category key<input id="cfwKey" maxlength="80" required placeholder="customer_receipts"></label><label>Display name<input id="cfwLabel" maxlength="120" required placeholder="Customer receipts"></label><button type="submit">Add category</button></form><div id="cfwCategories" class="cfw-category-list"></div></section><section class="cfw-panel" data-cfw-panel="accounts" hidden><div class="cfw-account-columns"><div><label>Search accounts<input id="cfwAccountSearch" type="search" placeholder="Code or account name"></label><div id="cfwAccountOptions" class="cfw-account-options"></div></div><aside><h3>Selected Cash accounts</h3><div id="cfwCashSelected"></div><h3>Selected Bank accounts</h3><div id="cfwBankSelected"></div></aside></div><button type="button" data-cfw-save-accounts>Save Cash &amp; Bank accounts</button></section><section class="cfw-panel" data-cfw-panel="settings" hidden><label class="cfw-check"><input id="cfwMatchTransfers" type="checkbox"> Match internal Cash-to-Cash and Bank-to-Bank transfers</label><label>Customer receipt presentation<select id="cfwReceipt"><option value="net">Net of withholding tax and bank charges</option><option value="gross">Gross receipt with deductions shown</option></select></label><div id="cfwRoles" class="cfw-role-grid"></div><button type="button" data-cfw-save-settings>Save settings</button></section><section class="cfw-panel" data-cfw-panel="template" hidden><label>Client Data Bank template<select id="cfwTemplateSelect"><option value="">Loading templates…</option></select></label><p id="cfwTemplateHint"></p><div id="cfwTemplateMapping"></div><button type="button" data-cfw-save-template disabled>Save template mapping</button></section></section></div>');
            setup = el('cfwBackdrop');
            setup.addEventListener('click', event => { if (event.target === setup || event.target.closest('[data-cfw-close]')) close(); });
            setup.querySelectorAll('[data-cfw-tab]').forEach(button => button.addEventListener('click', () => selectTab(button.dataset.cfwTab)));
            el('cfwCategoryForm').addEventListener('submit', createCategory);
            el('cfwCategories').addEventListener('click', categoryAction);
            el('cfwAccountSearch').addEventListener('input', renderAccounts);
            el('cfwAccountOptions').addEventListener('change', updateTreasury);
            setup.querySelector('[data-cfw-save-accounts]').addEventListener('click', saveTreasury);
            setup.querySelector('[data-cfw-save-settings]').addEventListener('click', saveSettings);
            el('cfwTemplateSelect').addEventListener('change', loadTemplateColumns);
            el('cfwTemplateMapping').addEventListener('change', updateTemplateSave);
            setup.querySelector('[data-cfw-save-template]').addEventListener('click', saveTemplate);
            document.addEventListener('keydown', event => { if (event.key === 'Escape' && setup && !setup.hidden) close(); });
        }
        function notice(message, error, keepInline = false) {
            const status = el('cfwStatus');
            if (!message) { status.textContent = ''; status.hidden = true; return; }
            if (!keepInline && host.showStatus) { status.hidden = true; host.showStatus(message, error ? 'error' : 'success'); return; }
            status.textContent = message; status.hidden = false;
            status.className = 'cfw-status' + (error ? ' error' : ' success');
        }
        function selectTab(tab) {
            currentTab = tab;
            setup.querySelectorAll('[data-cfw-tab]').forEach(button => button.setAttribute('aria-selected', String(button.dataset.cfwTab === tab)));
            setup.querySelectorAll('[data-cfw-panel]').forEach(panel => { panel.hidden = panel.dataset.cfwPanel !== tab; });
            if (tab === 'template') loadTemplates().catch(error => notice(error.message, true));
        }
        async function load() {
            state = await json('/api/account-control-center/cashflow/setup?' + query());
            renderCategories(); renderAccounts(); renderRoles();
            el('cfwMatchTransfers').checked = !!(state.settings && state.settings.match_internal_transfers);
            el('cfwReceipt').value = state.settings && state.settings.receipt_presentation || 'net';
        }
        async function open(tab) {
            if (!host.isAdmin) throw new Error('Only administrators can manage Cashflow setup.');
            ensure(); setup.hidden = false; document.body.classList.add('cfw-open'); notice('Loading Cashflow setup…', false, true);
            if (host.onModalChanged) host.onModalChanged(true);
            try { await load(); notice(''); selectTab(tab || 'categories'); el('cfwTitle').focus?.(); }
            catch (error) { notice(''); throw error; }
        }
        function close() { if (setup) setup.hidden = true; document.body.classList.remove('cfw-open'); if (host.onModalChanged) host.onModalChanged(false); if (host.onClose) host.onClose(); }
        function renderCategories() {
            const categories = state.categories || [];
            el('cfwCategories').innerHTML = categories.map((item, index) => '<article class="cfw-category"><div><strong>' + escapeHtml(item.label) + '</strong><small>' + escapeHtml(item.category_key) + (item.is_active ? '' : ' · Inactive') + '</small></div><div class="cfw-actions"><button type="button" data-action="rename" data-id="' + item.id + '">Rename</button><button type="button" data-action="up" data-id="' + item.id + '"' + (index === 0 ? ' disabled' : '') + '>↑</button><button type="button" data-action="down" data-id="' + item.id + '"' + (index === categories.length - 1 ? ' disabled' : '') + '>↓</button><button type="button" data-action="active" data-id="' + item.id + '">' + (item.is_active ? 'Deactivate' : 'Activate') + '</button></div></article>').join('') || '<p>No Cashflow categories yet.</p>';
        }
        async function createCategory(event) {
            event.preventDefault();
            try { await json('/api/account-control-center/cashflow/categories', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({client_id:clientId(), category_key:el('cfwKey').value.trim(), label:el('cfwLabel').value.trim(), sort_order:(state.categories||[]).length})});
                el('cfwCategoryForm').reset(); await load(); if (host.onSaved) await host.onSaved(); notice('Category added.');
            } catch (error) { notice(error.message, true); }
        }
        async function categoryAction(event) {
            const button = event.target.closest('[data-action]'); if (!button || button.disabled) return;
            const category = (state.categories || []).find(item => String(item.id) === button.dataset.id); if (!category) return;
            const action = button.dataset.action; let patch = {};
            if (action === 'rename') { const label = window.prompt('Display label', category.label); if (!label || !label.trim() || label.trim() === category.label) return; patch.label = label.trim(); }
            else if (action === 'active') patch.is_active = !category.is_active;
            else { const index = state.categories.indexOf(category), other = state.categories[index + (action === 'up' ? -1 : 1)]; if (!other) return;
                try { await updateCategory(category, {sort_order:other.sort_order}); await updateCategory(other, {sort_order:category.sort_order}); await load(); if (host.onSaved) await host.onSaved(); return; } catch(error) { notice(error.message,true); return; } }
            try { await updateCategory(category, patch); await load(); if (host.onSaved) await host.onSaved(); notice('Category updated.'); } catch (error) { notice(error.message, true); }
        }
        async function updateCategory(category, patch) {
            return json('/api/account-control-center/cashflow/categories/' + category.id, {method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({client_id:clientId(),label:patch.label == null ? category.label : patch.label,sort_order:patch.sort_order == null ? category.sort_order : patch.sort_order,is_active:patch.is_active == null ? category.is_active : patch.is_active})});
        }
        function visibleAccounts() {
            const term = el('cfwAccountSearch').value.trim().toLowerCase();
            return (state.accounts || []).filter(account => account.is_leaf && (!term || (account.account_code + ' ' + account.account_name).toLowerCase().includes(term)));
        }
        function renderAccounts() {
            if (!state) return;
            const selected = state.treasury_accounts || {cash:[],bank:[]};
            el('cfwAccountOptions').innerHTML = visibleAccounts().map(account => {
                const code = String(account.account_code).trim();
                const type = (selected.cash || []).includes(code) ? 'cash' : ((selected.bank || []).includes(code) ? 'bank' : '');
                return '<label class="cfw-account-option"><span><strong>' + escapeHtml(code) + '</strong> ' + escapeHtml(account.account_name) + '</span><select data-treasury-code="' + escapeHtml(code) + '"><option value="">Not selected</option><option value="cash"' + (type === 'cash' ? ' selected' : '') + '>Cash</option><option value="bank"' + (type === 'bank' ? ' selected' : '') + '>Bank</option></select></label>';
            }).join('') || '<p>No matching leaf accounts.</p>';
            ['cash','bank'].forEach(type => { const target = el(type === 'cash' ? 'cfwCashSelected' : 'cfwBankSelected'); target.textContent = (selected[type] || []).length ? (selected[type] || []).join(', ') : 'None selected'; });
        }
        function updateTreasury(event) {
            const select = event.target.closest('[data-treasury-code]'); if (!select) return;
            const code = select.dataset.treasuryCode, selected = state.treasury_accounts || {cash:[],bank:[]};
            for (const type of ['cash','bank']) selected[type] = (selected[type] || []).filter(item => item !== code);
            if (select.value) selected[select.value].push(code); state.treasury_accounts = selected; renderAccounts();
        }
        async function saveTreasury() {
            const selected = state.treasury_accounts || {cash:[],bank:[]};
            try { await json('/api/account-control-center/cashflow/treasury-accounts', {method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({client_id:clientId(),trial_balance_upload_id:uploadId(),cash_account_codes:selected.cash||[],bank_account_codes:selected.bank||[]})}); notice('Cash and Bank accounts saved.'); }
            catch (error) { notice(error.message,true); }
        }
        function renderRoles() {
            const roleCategories = state.settings && state.settings.role_categories || {};
            el('cfwRoles').innerHTML = ROLES.map(([key,label]) => '<fieldset><legend>' + escapeHtml(label) + '</legend>' + (state.categories || []).filter(item => item.is_active).map(item => '<label><input type="checkbox" data-role="' + key + '" value="' + item.id + '"' + ((roleCategories[key] || []).map(String).includes(String(item.id)) ? ' checked' : '') + '> ' + escapeHtml(item.label) + '</label>').join('') + '</fieldset>').join('');
        }
        async function saveSettings() {
            const payload = {client_id:clientId(),match_internal_transfers:el('cfwMatchTransfers').checked,receipt_presentation:el('cfwReceipt').value};
            ROLES.forEach(([key]) => { payload[key + '_category_ids'] = Array.from(setup.querySelectorAll('[data-role="' + key + '"]:checked'), input => Number(input.value)); });
            try { const result = await json('/api/account-control-center/cashflow/settings', {method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)}); state.settings = {...state.settings,...payload,role_categories:result.role_categories || state.settings.role_categories}; notice('Cashflow settings saved.'); }
            catch (error) { notice(error.message,true); }
        }
        async function loadTemplates() {
            const result = await json('/api/reconciliation/cashflows/sources?client_id=' + encodeURIComponent(clientId()));
            const byId = new Map((result.sources || []).map(source => [String(source.template_id), source]));
            const select = el('cfwTemplateSelect');
            select.innerHTML = '<option value="">Choose a template</option>' + Array.from(byId.values()).map(source => '<option value="' + escapeHtml(source.template_id) + '">' + escapeHtml(source.template_name || ('Template ' + source.template_id)) + '</option>').join('');
            select.value = String(state.settings && state.settings.source_template_id || '');
            el('cfwTemplateHint').textContent = byId.size ? byId.size + ' template(s) available.' : 'Upload a Client Data Bank template first.';
            el('cfwTemplateMapping').replaceChildren();
            if (select.value) await loadTemplateColumns();
        }
        async function loadTemplateColumns() {
            const id = el('cfwTemplateSelect').value; if (!id) { el('cfwTemplateMapping').replaceChildren(); updateTemplateSave(); return; }
            try {
                const result = await json('/api/reconciliation/cashflows/template-columns?client_id=' + encodeURIComponent(clientId()) + '&template_id=' + encodeURIComponent(id));
                el('cfwTemplateMapping').innerHTML = '<div class="cfw-template-head"><strong>Cashflow field</strong><strong>Template column</strong></div>' + (result.roles || []).map(role => '<label class="cfw-template-row"><span>' + escapeHtml(role.label) + (role.required ? ' · Required' : ' · Recommended') + '</span><select data-template-role="' + escapeHtml(role.role) + '" data-required="' + String(!!role.required) + '"><option value="">Not mapped</option>' + (result.columns || []).map(column => '<option value="' + column.template_column_id + '"' + (String(column.template_column_id) === String(role.template_column_id) ? ' selected' : '') + '>' + escapeHtml(column.column_name) + '</option>').join('') + '</select></label>').join('');
                updateTemplateSave();
            } catch (error) { el('cfwTemplateHint').textContent = error.message; }
        }
        function updateTemplateSave() {
            const selects = Array.from(el('cfwTemplateMapping').querySelectorAll('[data-template-role]'));
            setup.querySelector('[data-cfw-save-template]').disabled = !selects.length || selects.some(select => select.dataset.required === 'true' && !select.value);
        }
        async function saveTemplate() {
            const templateId = Number(el('cfwTemplateSelect').value || 0); if (!templateId) return;
            const mappings = Array.from(el('cfwTemplateMapping').querySelectorAll('[data-template-role]')).filter(select => select.value).map(select => ({role:select.dataset.templateRole,template_column_id:Number(select.value)}));
            try { const result = await json('/api/reconciliation/cashflows/template-columns', {method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({client_id:clientId(),template_id:templateId,mappings,set_as_default:true})}); state.settings.source_template_id = templateId; el('cfwTemplateHint').textContent = result.ready ? 'Template mapping saved.' : 'Map all required fields before saving.'; }
            catch (error) { el('cfwTemplateHint').textContent = error.message; }
        }
        function categorySelect(account, categories, selected) {
            const options = categoryOptions(categories, selected);
            return '<select class="tb-cashflow-category-picker" data-cashflow-code="' + escapeHtml(account.account_code) + '" data-cashflow-parent="' + String(!account.is_leaf) + '" aria-label="Cashflow category for ' + escapeHtml(account.account_code) + '">' + options + '</select>';
        }
        return Object.freeze({open,close,load,categorySelect,get state(){return state;},selectTab});
    }
    function categoryOptions(categories, selected) {
        const selectedValue = selected == null ? '' : String(selected);
        let html = '<option value="">Unassigned</option>';
        if (selectedValue === '__mixed') html = '<option value="__mixed" selected>Mixed assignments — choose one</option>' + html;
        return html + (categories || []).map(category => '<option value="' + category.id + '"' + (String(category.id) === selectedValue ? ' selected' : '') + (!category.is_active && String(category.id) !== selectedValue ? ' disabled' : '') + '>' + escapeHtml(category.label) + (!category.is_active ? ' · Inactive' : '') + '</option>').join('');
    }
    window.CashflowWorkspace = Object.freeze({create,categoryOptions});
})();
