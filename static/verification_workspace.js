/* In-place Verification assignment controls shared by the Trial Balance workspace. */
(function () {
    'use strict';

    const escapeHtml = value => String(value == null ? '' : value).replace(/[&<>\'"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[c]));

    function selectMarkup(account, selected, workstreams) {
        const disabled = !account.is_leaf && !account.has_leaf_accounts;
        const options = ['<option value="">Unassigned</option>'];
        if (selected === '__mixed') options.push('<option value="__mixed" selected>Mixed assignments — choose one</option>');
        (workstreams || []).forEach(item => {
            options.push('<option value="' + escapeHtml(item.value) + '"' + (String(selected) === String(item.value) ? ' selected' : '') + '>' + escapeHtml(item.label) + '</option>');
        });
        return '<select class="tb-verification-workstream-picker" data-verification-code="' + escapeHtml(account.account_code) + '" data-verification-parent="' + String(!account.is_leaf) + '" aria-label="Verification workstream for ' + escapeHtml(account.account_code) + '"' + (disabled ? ' disabled title="No linked leaf accounts. Correct the account code first."' : '') + '>' + options.join('') + '</select>';
    }

    function create(host) {
        let backdrop = null;
        let accounts = [];
        let leavesByParent = new Map();
        const el = id => document.getElementById(id);
        const workstreams = () => window.AccountAssignments ? AccountAssignments.workstreams : [];

        function ensure() {
            if (backdrop) return;
            document.head.insertAdjacentHTML('beforeend', '<style id="verificationWorkspaceStyles">' + (host.styles || '') + '</style>');
            document.body.insertAdjacentHTML('beforeend', '<div class="vw-backdrop" id="vwBackdrop" hidden><section class="vw-modal" role="dialog" aria-modal="true" aria-labelledby="vwTitle"><header class="vw-head"><div><h2 id="vwTitle">Verification assignments</h2><p>Assign Trial Balance accounts to the workstream used to find their vouchers.</p></div><button type="button" class="vw-close" data-vw-close aria-label="Close Verification assignments">×</button></header><div id="vwStatus" class="vw-status" role="status" hidden></div><label class="vw-search">Search accounts<input id="vwSearch" type="search" placeholder="Account code or name"></label><div class="vw-summary" id="vwSummary"></div><div class="vw-table-wrap"><table class="vw-table"><thead><tr><th>Account code</th><th>Account name</th><th>Type</th><th>Verification workstream</th></tr></thead><tbody id="vwRows"></tbody></table></div></section></div>');
            backdrop = el('vwBackdrop');
            backdrop.addEventListener('click', event => { if (event.target === backdrop || event.target.closest('[data-vw-close]')) close(); });
            el('vwSearch').addEventListener('input', render);
            el('vwRows').addEventListener('change', handleChange);
            document.addEventListener('keydown', event => { if (event.key === 'Escape' && backdrop && !backdrop.hidden) close(); });
        }

        function status(message, kind) {
            const target = el('vwStatus');
            if (!message) {
                target.textContent = '';
                target.hidden = true;
                target.className = 'vw-status';
                return;
            }
            if ((kind === 'success' || kind === 'error') && host.showStatus) {
                target.textContent = '';
                target.hidden = true;
                host.showStatus(message, kind);
                return;
            }
            target.textContent = message || '';
            target.hidden = !message;
            target.className = 'vw-status' + (kind ? ' ' + kind : '');
        }

        function indexLeaves(source) {
            const byCode = new Map(source.map(account => [String(account.account_code || '').trim(), account]));
            const index = new Map();
            source.filter(account => account.is_leaf).forEach(leaf => {
                let parentCode = String(leaf.parent_account_code || '').trim();
                const seen = new Set();
                while (parentCode && !seen.has(parentCode)) {
                    seen.add(parentCode);
                    const children = index.get(parentCode) || [];
                    children.push(leaf); index.set(parentCode, children);
                    const parent = byCode.get(parentCode);
                    parentCode = parent ? String(parent.parent_account_code || '').trim() : '';
                }
            });
            return index;
        }

        function effective(account) {
            const direct = account.assignment && account.assignment.verification_workstream;
            if (account.is_leaf || direct) return direct || '';
            const values = Array.from(new Set((leavesByParent.get(String(account.account_code || '').trim()) || []).map(leaf => {
                const value = leaf.assignment && leaf.assignment.verification_workstream;
                return value == null ? '' : String(value);
            })));
            return values.length === 1 ? values[0] : values.length > 1 ? '__mixed' : '';
        }

        function render() {
            const search = el('vwSearch').value.trim().toLowerCase();
            const visible = accounts.filter(account => !search || (String(account.account_code || '') + ' ' + String(account.account_name || '')).toLowerCase().includes(search));
            el('vwSummary').textContent = visible.length + ' of ' + accounts.length + ' accounts';
            el('vwRows').innerHTML = visible.map(account => {
                const parent = !account.is_leaf;
                const leaves = leavesByParent.get(String(account.account_code || '').trim()) || [];
                const noLeaves = parent && leaves.length === 0;
                const selected = effective(account);
                const type = parent ? 'Parent' : 'Leaf';
                const name = account.account_name || 'Unknown account';
                const rowClass = (parent ? 'vw-parent ' : '') + (noLeaves ? 'vw-unlinked' : '');
                return '<tr' + (rowClass ? ' class="' + rowClass.trim() + '"' : '') + '><td>' + escapeHtml(account.account_code) + (noLeaves ? '<small>Check code</small>' : '') + '</td><td>' + escapeHtml(name) + '</td><td>' + type + '</td><td>' + selectMarkup(Object.assign({}, account, {has_leaf_accounts: leaves.length > 0}), selected, workstreams()) + '</td></tr>';
            }).join('') || '<tr><td colspan="4" class="vw-empty">No matching accounts.</td></tr>';
        }

        async function load() {
            const result = await host.readMapping();
            if (String(result.client_id) !== String(host.clientId()) || !result.upload || String(result.upload.id) !== String(host.uploadId())) {
                throw new Error('Verification assignments do not match the selected Trial Balance.');
            }
            accounts = Array.isArray(result.accounts) ? result.accounts : [];
            leavesByParent = indexLeaves(accounts);
            render();
            return result;
        }

        async function save(account, select) {
            if (!host.isAdmin()) throw new Error('Only administrators can manage Verification assignments.');
            const parent = !account.is_leaf;
            const leaves = leavesByParent.get(String(account.account_code || '').trim()) || [];
            if (parent && !leaves.length) throw new Error('No linked leaf accounts. Correct the account code first.');
            if (parent && !window.confirm('Apply this Verification workstream to all ' + leaves.length + ' leaf accounts below ' + account.account_code + '?')) {
                render(); return;
            }
            select.disabled = true;
            status(parent ? 'Applying assignment to child accounts…' : 'Saving assignment…');
            try {
                await host.save(account, select.value || null, parent);
                const refreshed = host.onSaved ? await host.onSaved({accountCode:account.account_code,workstream:select.value || null,parent}) : null;
                if (refreshed && Array.isArray(refreshed.accounts)) {
                    accounts = refreshed.accounts; leavesByParent = indexLeaves(accounts); render();
                } else await load();
                status(parent ? 'Assignment applied to the leaf accounts.' : 'Assignment saved.', 'success');
            } catch (error) {
                status(error.message || 'Verification assignment could not be saved.', 'error');
                await load().catch(() => {});
            }
        }

        function handleChange(event) {
            const select = event.target.closest('[data-verification-code]');
            if (!select) return;
            const account = accounts.find(item => String(item.account_code || '').trim() === select.dataset.verificationCode);
            if (account) save(account, select);
        }

        async function open() {
            if (!host.isAdmin()) throw new Error('Only administrators can manage Verification assignments.');
            if (!host.uploadId()) throw new Error('Select a Trial Balance first.');
            ensure(); backdrop.hidden = false; document.body.classList.add('vw-open');
            if (host.onModalChanged) host.onModalChanged(true);
            status('Loading Verification assignments…');
            try { await load(); status(''); el('vwSearch').focus(); }
            catch (error) { status(''); throw error; }
        }

        function close() {
            if (backdrop) backdrop.hidden = true;
            document.body.classList.remove('vw-open');
            if (host.onModalChanged) host.onModalChanged(false);
        }

        return Object.freeze({open, close, selectMarkup, get accounts() { return accounts; }});
    }

    window.VerificationWorkspace = Object.freeze({create, selectMarkup});
})();
