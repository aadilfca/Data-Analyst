/* Shared account-assignment context and read transport for TB and ACC. */
(function () {
    'use strict';
    const workstreams = Object.freeze([
        {value: 'cash', label: 'Cash vouchers'}, {value: 'bank', label: 'Bank vouchers'},
        {value: 'local_vendor', label: 'Local vendor'}, {value: 'import_vendor', label: 'Import vendor'},
        {value: 'export_customer', label: 'Export customer'}, {value: 'payroll', label: 'Payroll'},
        {value: 'contractor', label: 'Contractor'},
    ].map(Object.freeze));
    const workflows = Object.freeze({
        financial: Object.freeze({label: 'Financial Statements', column: 'FS Category', tab: 'financial', setups: [['financial', 'Financial Statements setup'], ['notes', 'Notes setup']]}),
        cashflow: Object.freeze({label: 'Cashflows', column: 'Cashflow Category', tab: 'cashflow', setups: [['cashflow', 'Cashflow categories'], ['treasury', 'Cash & Bank configuration'], ['template', 'Transaction template']]}),
        verification: Object.freeze({label: 'Verification', column: 'Verification Workstream', tab: 'verification', setups: [['verification', 'Verification accounts']]}),
    });
    const requests = new WeakMap();
    function normaliseCode(value) { return String(value == null ? '' : value).trim(); }
    function mode(value) { return value === 'notes' ? 'financial' : Object.hasOwn(workflows, value) ? value : 'financial'; }
    function read(kind, clientId, uploadId, request) {
        const path = kind === 'cashflow' ? 'cashflow/setup' : 'mapping';
        const query = new URLSearchParams({client_id: String(clientId)});
        if (uploadId) query.set('trial_balance_upload_id', String(uploadId));
        const url = '/api/account-control-center/' + path + '?' + query;
        if (!requests.has(request)) requests.set(request, new Map());
        const pending = requests.get(request);
        if (pending.has(url)) return pending.get(url);
        const promise = Promise.resolve().then(() => request(url)).then(function (result) {
            if (!result || (result.client_id != null && String(result.client_id) !== String(clientId))
                || !result.upload || (uploadId && String(result.upload.id) !== String(uploadId)) || !Array.isArray(result.accounts)) {
                throw new Error('Assignment response does not match the selected client and Trial Balance.');
            }
            return result;
        }).finally(() => { if (pending.get(url) === promise) pending.delete(url); });
        pending.set(url, promise);
        return promise;
    }
    function accUrl(workflow, uploadId, setup) {
        const selected = mode(workflow);
        const allowed = workflows[selected].setups.some(item => item[0] === setup);
        const params = new URLSearchParams({tab: setup === 'notes' ? 'notes' : workflows[selected].tab, from: 'trial-balance', workflow: selected});
        if (uploadId) params.set('upload_id', String(uploadId));
        if (allowed) params.set('setup', setup);
        return '/static/account_control_center.html?' + params;
    }
    function trialBalanceUrl(tab, uploadId, restore) {
        const params = new URLSearchParams({view: 'available', acc_tab: tab, workflow: mode(tab)});
        if (uploadId) params.set('upload_id', String(uploadId));
        if (restore) params.set('restore', '1');
        return '/static/trial_balance.html?' + params;
    }
    function invalidate(request) { requests.delete(request); }
    window.AccountAssignments = Object.freeze({workstreams, workflows, mode, normaliseCode, read, invalidate, accUrl, trialBalanceUrl});
})();
