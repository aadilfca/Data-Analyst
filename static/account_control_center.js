// Account Control Centre: guided editor for the versioned Account Set APIs.
let accClientId = null;
let accAccounts = [];
let accLatestUpload = null;
let accSets = [];
let accSelectedSet = null;
let accSelectedVersionId = null;
let accPreviewRows = [];

const ACC_SELECTOR_LABELS = {
    include_subtree: 'Include family', include_exact: 'Include account',
    exclude_subtree: 'Exclude family', exclude_exact: 'Exclude account',
};

const ACC_STARTERS = [
    { key: 'cash_accounts', name: 'Cash accounts', purpose: 'Cash in hand, petty cash and cash collection accounts.' },
    { key: 'bank_accounts', name: 'Bank accounts', purpose: 'All operating and collection bank accounts.' },
    { key: 'local_vendors', name: 'Local vendors', purpose: 'Local raw material, packing and finished-goods creditors.' },
    { key: 'import_vendors', name: 'Import vendors', purpose: 'Imported raw material and LC creditor accounts.' },
    { key: 'export_customers', name: 'Export customers', purpose: 'Trade debtors and export customer accounts.' },
    { key: 'payroll', name: 'Payroll', purpose: 'Production wages and administration salaries.' },
    { key: 'contractors', name: 'Contractors', purpose: 'Contractor and outsourced-work payables.' },
];

function accElement(id) { return document.getElementById(id); }
function accEscape(value) {
    return String(value ?? '').replace(/[&<>'"]/g, character => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;' }[character]));
}
function accShowStatus(message, type = 'info') {
    const status = accElement('accStatus');
    status.textContent = message;
    status.className = `acc-status ${type === 'info' ? '' : type}`.trim();
    status.style.display = 'block';
}
function accClearStatus() { accElement('accStatus').style.display = 'none'; }
async function accApi(url, options = {}) {
    const response = await fetch(url, options);
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(payload.detail || payload.message || 'The request could not be completed.');
    return payload;
}
function accCurrentVersion() { return accSelectedSet?.versions?.find(version => version.id === accSelectedVersionId) || null; }
function accIsDraft() { return accCurrentVersion()?.status === 'draft'; }
function accNormaliseKey(value) { return value.toLowerCase().trim().replace(/[^a-z0-9]+/g, '_').replace(/^_+|_+$/g, ''); }
function accSourceText(upload) {
    if (!upload) return 'Source Trial Balance: unavailable.';
    return `Source Trial Balance: ${upload.file_name || 'Trial Balance upload'}${upload.as_of_date ? ` as at ${upload.as_of_date}` : ''}.`;
}
function accSyncDraftFields() {
    const version = accCurrentVersion();
    if (!version || version.status !== 'draft') return;
    version.display_name = accElement('accName').value.trim();
    version.description = accElement('accDescription').value.trim() || null;
}
function accSetStatus(set) {
    if (set?.active_version_status === 'published' || set?.active_version_id) return 'published';
    return set ? 'draft' : 'not configured';
}
function accRenderReadiness() {
    const panel = accElement('accReadiness');
    const leafCount = accAccounts.filter(account => account.is_leaf).length;
    if (accLatestUpload) {
        panel.className = 'acc-readiness';
        panel.innerHTML = `<div><span class="acc-readiness-title">Trial Balance ready</span><span class="acc-readiness-copy">${accEscape(accLatestUpload.file_name || 'Latest classified Trial Balance')}${accLatestUpload.as_of_date ? ` as at ${accEscape(accLatestUpload.as_of_date)}` : ''} is the source for new and revised Account Sets.</span></div><span class="acc-readiness-stat">${leafCount} leaf accounts available</span>`;
        return;
    }
    panel.className = 'acc-readiness error';
    panel.innerHTML = '<div><span class="acc-readiness-title">Trial Balance required</span><span class="acc-readiness-copy">Upload and classify a Trial Balance before setting up a workstream.</span></div><span class="acc-readiness-stat">No source accounts</span>';
}
function accRenderStarterGrid() {
    const grid = accElement('accStarterGrid');
    grid.innerHTML = ACC_STARTERS.map(starter => {
        const set = accSets.find(item => item.set_key === starter.key);
        const status = accSetStatus(set);
        const action = set ? 'Manage' : 'Set up';
        return `<article class="acc-starter-card"><h3>${accEscape(starter.name)}</h3><p>${accEscape(starter.purpose)}</p><span class="acc-starter-status ${status === 'not configured' ? '' : status}">${accEscape(status)}</span><br><button type="button" class="acc-card-btn" data-acc-starter-key="${starter.key}">${action}</button></article>`;
    }).join('');
}
function accRenderSetList() {
    const list = accElement('accSetList');
    if (!accSets.length) {
        list.innerHTML = '<div class="acc-empty">No Account Sets have been created yet.</div>';
        return;
    }
    list.innerHTML = accSets.map(set => {
        const selected = set.id === accSelectedSet?.id ? ' selected' : '';
        const label = set.display_name || set.set_key;
        const version = set.version_number ? `Published version ${set.version_number}` : 'Draft in progress';
        return `<button type="button" class="acc-set-item${selected}" data-acc-set-id="${set.id}"><span class="acc-set-name">${accEscape(label)}</span><span class="acc-set-meta">${accEscape(version)} | ${accEscape(accSetStatus(set))}</span></button>`;
    }).join('');
}
function accRenderLifecycle() {
    const steps = document.querySelectorAll('.acc-lifecycle span');
    const version = accCurrentVersion();
    const active = version?.status === 'published' ? 3 : (accPreviewRows.length ? 2 : 1);
    steps.forEach((step, index) => step.classList.toggle('active', index + 1 === active));
}
function accRenderSelectionSummary() {
    const target = accElement('accSelectionSummary');
    const selectors = accCurrentVersion()?.selectors || [];
    const inclusions = selectors.filter(selector => selector.selector_type.startsWith('include')).length;
    const exclusions = selectors.length - inclusions;
    if (!selectors.length) {
        target.textContent = 'No account rules selected yet. Start by including a complete account family or a specific leaf account.';
        return;
    }
    target.textContent = `${inclusions} inclusion rule${inclusions === 1 ? '' : 's'}${exclusions ? ` and ${exclusions} exclusion rule${exclusions === 1 ? '' : 's'}` : ''}. Save and preview to confirm the final leaf-account list.`;
}
function accRenderEditor() {
    const editor = accElement('accEditor');
    const empty = accElement('accEmptyEditor');
    const createPanel = accElement('accCreatePanel');
    const version = accCurrentVersion();
    if (!accSelectedSet || !version) {
        editor.hidden = true;
        if (createPanel.hidden) empty.hidden = false;
        return;
    }
    createPanel.hidden = true;
    editor.hidden = false;
    empty.hidden = true;
    const isDraft = version.status === 'draft';
    accElement('accEditorTitle').textContent = version.display_name;
    accElement('accEditorKey').textContent = `Stable key: ${accSelectedSet.set_key}`;
    const badge = accElement('accVersionStatus');
    badge.textContent = `${version.status} | version ${version.version_number}`;
    badge.className = `acc-badge ${version.status}`;
    accElement('accVersionTabs').innerHTML = accSelectedSet.versions.map(item => `<button type="button" class="acc-version-tab${item.id === version.id ? ' selected' : ''}" data-acc-version-id="${item.id}">Version ${item.version_number} | ${accEscape(item.status)}</button>`).join('');
    accElement('accName').value = version.display_name || '';
    accElement('accDescription').value = version.description || '';
    accElement('accName').disabled = !isDraft;
    accElement('accDescription').disabled = !isDraft;
    accElement('accSourceInfo').textContent = accSourceText(version.source_upload_id === accLatestUpload?.id ? accLatestUpload : { id: version.source_upload_id });
    accRenderLifecycle();
    accRenderActions();
    accRenderSelectionSummary();
    accRenderSelectors();
    accRenderAccountTree();
    accRenderPreview();
}
function accRenderActions() {
    const actions = accElement('accVersionActions');
    const version = accCurrentVersion();
    if (!version) return;
    if (version.status === 'draft') {
        actions.innerHTML = '<button id="accSaveDraftBtn" type="button" class="acc-btn primary">1. Save account rules</button><button id="accPreviewBtn" type="button" class="acc-btn">2. Preview leaf accounts</button><button id="accPublishBtn" type="button" class="acc-btn publish">3. Publish version</button>';
        return;
    }
    actions.innerHTML = '<button id="accRevisionBtn" type="button" class="acc-btn primary">Create revision draft</button><span class="acc-hint" style="margin:0;">Published versions are retained as read-only audit records.</span>';
}
function accRenderSelectors() {
    const version = accCurrentVersion();
    const list = accElement('accSelectorList');
    if (!version?.selectors?.length) {
        list.innerHTML = '<span class="acc-empty">No rules selected yet.</span>';
        return;
    }
    list.innerHTML = version.selectors.map((selector, index) => {
        const exclude = selector.selector_type.startsWith('exclude') ? ' exclude' : '';
        const remove = accIsDraft() ? `<button type="button" data-acc-remove-selector="${index}" aria-label="Remove rule">x</button>` : '';
        return `<span class="acc-selector${exclude}">${accEscape(ACC_SELECTOR_LABELS[selector.selector_type])}: ${accEscape(selector.account_code)}${remove}</span>`;
    }).join('');
}
function accRenderAccountTree() {
    const tbody = accElement('accAccountTree');
    const filter = accElement('accAccountFilter').value.trim().toLowerCase();
    const visible = accAccounts.filter(account => !filter || `${account.account_code} ${account.account_name || ''}`.toLowerCase().includes(filter));
    accElement('accTreeCount').textContent = `${visible.length} of ${accAccounts.length} accounts`;
    if (!visible.length) { tbody.innerHTML = '<tr><td colspan="3" class="acc-empty">No accounts match this search.</td></tr>'; return; }
    const editable = accIsDraft();
    tbody.innerHTML = visible.map(account => {
        const indent = Math.max(0, Number(account.level || 1) - 1) * 16;
        const actions = !editable ? '<span class="acc-hint" style="margin:0;">Published</span>' : account.is_leaf
            ? `<button type="button" class="acc-mini-btn" data-acc-selector-type="include_exact" data-acc-code="${accEscape(account.account_code)}">Include</button><button type="button" class="acc-mini-btn exclude" data-acc-selector-type="exclude_exact" data-acc-code="${accEscape(account.account_code)}">Exclude</button>`
            : `<button type="button" class="acc-mini-btn" data-acc-selector-type="include_subtree" data-acc-code="${accEscape(account.account_code)}">Include family</button><button type="button" class="acc-mini-btn exclude" data-acc-selector-type="exclude_subtree" data-acc-code="${accEscape(account.account_code)}">Exclude family</button>`;
        return `<tr class="${account.is_leaf ? '' : 'acc-parent'}"><td class="acc-tree-code">${accEscape(account.account_code)}</td><td class="acc-tree-name" style="padding-left:${indent + 9}px;">${accEscape(account.account_name || 'Unknown account')}</td><td><div class="acc-row-actions">${actions}</div></td></tr>`;
    }).join('');
}
function accRenderPreview() {
    const tbody = accElement('accPreviewTable');
    const count = accElement('accPreviewCount');
    if (!accPreviewRows.length) {
        count.textContent = 'Preview after saving account rules';
        tbody.innerHTML = '<tr><td colspan="3" class="acc-empty">No resolved leaf accounts to display.</td></tr>';
        accRenderLifecycle();
        return;
    }
    count.textContent = `${accPreviewRows.length} resolved leaf account${accPreviewRows.length === 1 ? '' : 's'}`;
    tbody.innerHTML = accPreviewRows.map(account => `<tr><td class="acc-tree-code">${accEscape(account.account_code)}</td><td>${accEscape(account.account_name || 'Unknown account')}</td><td>${accEscape(account.matched_by || 'Rule')}</td></tr>`).join('');
    accRenderLifecycle();
}
function accAddSelector(selectorType, accountCode) {
    const version = accCurrentVersion();
    if (!version || !accIsDraft()) return;
    accSyncDraftFields();
    if (version.selectors.some(selector => selector.selector_type === selectorType && selector.account_code === accountCode)) {
        accShowStatus('That account rule is already part of this draft.');
        return;
    }
    version.selectors.push({ selector_type: selectorType, account_code: accountCode, sort_order: version.selectors.length });
    accPreviewRows = [];
    accRenderEditor();
}
function accRemoveSelector(index) {
    const version = accCurrentVersion();
    if (!version || !accIsDraft()) return;
    accSyncDraftFields();
    version.selectors.splice(index, 1);
    version.selectors.forEach((selector, sortOrder) => { selector.sort_order = sortOrder; });
    accPreviewRows = [];
    accRenderEditor();
}
async function accLoadAccountTree() {
    try {
        const result = await accApi(`/api/account-control-center/accounts/latest?client_id=${encodeURIComponent(accClientId)}`);
        accAccounts = result.accounts || [];
        accLatestUpload = result.upload || null;
        accRenderReadiness();
        return true;
    } catch (error) {
        accAccounts = [];
        accLatestUpload = null;
        accRenderReadiness();
        if (!/No parsed Trial Balance/i.test(error.message)) throw error;
        accShowStatus('Upload and classify a Trial Balance before creating or revising Account Sets.', 'error');
        return false;
    }
}
async function accRefreshSets(preferredSetId = null, preferredVersionId = null) {
    const result = await accApi(`/api/account-control-center/sets?client_id=${encodeURIComponent(accClientId)}`);
    accSets = result.account_sets || [];
    accRenderStarterGrid();
    accRenderSetList();
    const targetId = preferredSetId || accSelectedSet?.id;
    if (targetId && accSets.some(set => set.id === targetId)) await accSelectSet(targetId, preferredVersionId || accSelectedVersionId);
    else { accSelectedSet = null; accSelectedVersionId = null; accPreviewRows = []; accRenderEditor(); }
}
async function accSelectSet(accountSetId, versionId = null) {
    accClearStatus();
    const detail = await accApi(`/api/account-control-center/sets/${accountSetId}`);
    accSelectedSet = detail;
    const draft = detail.versions.find(version => version.status === 'draft');
    accSelectedVersionId = versionId && detail.versions.some(version => version.id === versionId) ? versionId : (draft?.id || detail.active_version_id || detail.versions[0]?.id || null);
    accPreviewRows = [];
    accRenderStarterGrid();
    accRenderSetList();
    accRenderEditor();
    if (accCurrentVersion()?.selectors?.length) await accPreviewCurrent(false);
}
async function accSaveDraft(showSuccess = true) {
    const version = accCurrentVersion();
    if (!version || !accIsDraft()) return;
    accSyncDraftFields();
    if (!version.display_name) throw new Error('Account Set name is required.');
    const detail = await accApi(`/api/account-control-center/versions/${version.id}`, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ display_name: version.display_name, description: version.description, source_upload_id: version.source_upload_id, selectors: version.selectors.map((selector, sortOrder) => ({ ...selector, sort_order: sortOrder })) }) });
    accSelectedSet = detail;
    accSelectedVersionId = version.id;
    await accRefreshSets(detail.id, version.id);
    if (showSuccess) accShowStatus('Draft saved. Preview the resolved leaf accounts before publishing.', 'success');
}
async function accPreviewCurrent(saveFirst = true) {
    if (!accCurrentVersion()) return;
    if (saveFirst && accIsDraft()) await accSaveDraft(false);
    const version = accCurrentVersion();
    const result = await accApi(`/api/account-control-center/versions/${version.id}/preview`);
    accPreviewRows = result.resolved_leaf_accounts || [];
    accRenderPreview();
    if (!accPreviewRows.length) accShowStatus('This version resolves no leaf accounts. Add an inclusion rule before publishing.', 'error');
}
function accOpenCreate(starter = null) {
    accSelectedSet = null;
    accSelectedVersionId = null;
    accPreviewRows = [];
    accElement('accEditor').hidden = true;
    accElement('accEmptyEditor').hidden = true;
    accElement('accCreatePanel').hidden = false;
    accElement('accCreateTitle').textContent = starter ? `Set up ${starter.name}` : 'Create a custom Account Set';
    accElement('accNewName').value = starter?.name || '';
    accElement('accNewKey').value = starter?.key || '';
    accElement('accNewKey').dataset.userEdited = starter ? 'true' : '';
    accElement('accNewDescription').value = starter?.purpose || '';
    accElement('accNewName').focus();
}
function accCloseCreate() {
    accElement('accCreateForm').reset();
    accElement('accCreatePanel').hidden = true;
    accElement('accEmptyEditor').hidden = false;
}
async function accCreateSet(event) {
    event.preventDefault();
    const name = accElement('accNewName').value.trim();
    const key = accNormaliseKey(accElement('accNewKey').value);
    if (!name || !key) { accShowStatus('Both an Account Set name and stable key are required.', 'error'); return; }
    const detail = await accApi('/api/account-control-center/sets', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ client_id: accClientId, set_key: key, display_name: name, description: accElement('accNewDescription').value.trim() || null, selectors: [] }) });
    accCloseCreate();
    const draft = detail.versions.find(version => version.status === 'draft');
    await accRefreshSets(detail.id, draft?.id);
    accShowStatus('Draft created. Continue by selecting the accounts that belong in this workstream.', 'success');
}
async function accCreateRevision() {
    const detail = await accApi(`/api/account-control-center/sets/${accSelectedSet.id}/draft`, { method: 'POST' });
    const draft = detail.versions.find(version => version.status === 'draft');
    await accRefreshSets(detail.id, draft?.id);
    accShowStatus('Revision draft created from the published version and linked to the latest Trial Balance.', 'success');
}
async function accPublishCurrent() {
    const version = accCurrentVersion();
    if (!version || !accIsDraft()) return;
    if (!window.confirm('Publish this version? Published Account Set versions cannot be edited.')) return;
    await accSaveDraft(false);
    const detail = await accApi(`/api/account-control-center/versions/${version.id}/publish`, { method: 'POST' });
    await accRefreshSets(detail.id, version.id);
    accShowStatus('Account Set version published. Future changes require a separate revision draft.', 'success');
}
function accBindEvents() {
    accElement('accNewSetBtn').addEventListener('click', () => accOpenCreate());
    accElement('accCancelCreateBtn').addEventListener('click', accCloseCreate);
    accElement('accNewName').addEventListener('input', event => { const key = accElement('accNewKey'); if (!key.dataset.userEdited) key.value = accNormaliseKey(event.target.value); });
    accElement('accNewKey').addEventListener('input', event => { event.target.dataset.userEdited = 'true'; });
    accElement('accCreateForm').addEventListener('submit', event => { accCreateSet(event).catch(error => accShowStatus(error.message, 'error')); });
    accElement('accStarterGrid').addEventListener('click', event => { const button = event.target.closest('[data-acc-starter-key]'); if (!button) return; const starter = ACC_STARTERS.find(item => item.key === button.dataset.accStarterKey); const set = accSets.find(item => item.set_key === starter.key); if (set) accSelectSet(set.id).catch(error => accShowStatus(error.message, 'error')); else accOpenCreate(starter); });
    accElement('accSetList').addEventListener('click', event => { const button = event.target.closest('[data-acc-set-id]'); if (button) accSelectSet(Number(button.dataset.accSetId)).catch(error => accShowStatus(error.message, 'error')); });
    accElement('accVersionTabs').addEventListener('click', event => { const button = event.target.closest('[data-acc-version-id]'); if (!button) return; accSyncDraftFields(); accSelectedVersionId = Number(button.dataset.accVersionId); accPreviewRows = []; accRenderEditor(); if (accCurrentVersion()?.selectors?.length) accPreviewCurrent(false).catch(error => accShowStatus(error.message, 'error')); });
    accElement('accSelectorList').addEventListener('click', event => { const button = event.target.closest('[data-acc-remove-selector]'); if (button) accRemoveSelector(Number(button.dataset.accRemoveSelector)); });
    accElement('accAccountTree').addEventListener('click', event => { const button = event.target.closest('[data-acc-selector-type]'); if (button) accAddSelector(button.dataset.accSelectorType, button.dataset.accCode); });
    accElement('accAccountFilter').addEventListener('input', accRenderAccountTree);
    accElement('accVersionActions').addEventListener('click', event => { const actions = { accSaveDraftBtn: () => accSaveDraft(), accPreviewBtn: () => accPreviewCurrent(true), accPublishBtn: () => accPublishCurrent(), accRevisionBtn: () => accCreateRevision() }; const action = actions[event.target.id]; if (action) action().catch(error => accShowStatus(error.message, 'error')); });
}
async function initAccountControlCentre() {
    if (window.clientContextReady) await window.clientContextReady;
    accClientId = window.getSelectedClientId?.();
    if (!accClientId) return;
    accBindEvents();
    if (!await window.isAdminUser()) { accElement('accAdminWarning').style.display = 'block'; accElement('accNewSetBtn').disabled = true; return; }
    try { await accLoadAccountTree(); await accRefreshSets(); } catch (error) { accShowStatus(error.message, 'error'); }
}
document.addEventListener('DOMContentLoaded', initAccountControlCentre);
