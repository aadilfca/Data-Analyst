// Loaded synchronously by the .NET host before retained page scripts.
(() => {
    const tag = document.currentScript;
    const key = tag.dataset.context || '';
    const tenant = tag.dataset.tenant || '';
    if (tag.dataset.settingsEnabled === 'true') window.AppTenantSettings = {
        enabled: true, canEdit: tag.dataset.settingsAdmin === 'true'
    };
    if (tag.dataset.columnsEnabled === 'true') window.AppTenantColumns = {
        enabled: true, canEdit: tag.dataset.settingsAdmin === 'true'
    };
    if (tag.dataset.templatesEnabled === 'true') window.AppTenantTemplates = {
        enabled: true, canEdit: tag.dataset.settingsAdmin === 'true'
    };
    if (tag.dataset.mappingsEnabled === 'true') window.AppTenantMappings = { enabled: true };
    // Presence identifies the native host even when backup is disabled, so its
    // Setup screen cannot fall through into the legacy transfer protocol.
    window.AppConfigurationBackup = { enabled: tag.dataset.backupsEnabled === 'true', canEdit: tag.dataset.settingsAdmin === 'true' };
    window.AppNativeUpload = { enabled: tag.dataset.uploadEnabled === 'true', maximumFileBytes: Number(tag.dataset.uploadLimit) };
    window.AppNativeDataBank = { enabled: tag.dataset.databankEnabled === 'true', history: tag.dataset.historyEnabled === 'true',
        exports: tag.dataset.exportsEnabled === 'true', canBackup: tag.dataset.settingsAdmin === 'true', maximumExportBytes: Number(tag.dataset.exportLimit) };
    const marker = key + '/' + tenant;
    let changed = true;
    try {
        changed = sessionStorage.getItem('app-shell-context') !== marker;
        if (changed) {
            // This dedicated application origin uses sessionStorage for page
            // caches, work scopes and Back history. None may cross workspaces.
            sessionStorage.clear();
            sessionStorage.setItem('app-shell-context', marker);
            ['selectedClientId', 'selectedClientName', 'selectedClientCode'].forEach(k => localStorage.removeItem(k));
        }
    } catch (error) { console.warn('Workspace browser state could not be cleared'); }

    // Same-origin tabs share a session cookie. Notify them immediately when a
    // newly served document observes a different session or tenant revision.
    try {
        const channel = new BroadcastChannel('app-shell-context');
        channel.addEventListener('message', event => {
            if (event.data.marker !== marker) {
                document.documentElement.style.visibility = 'hidden';
                location.replace(event.data.authenticated ? '/shell/tenant_select.html' : '/static/login.html');
            }
        });
        if (changed) channel.postMessage({ marker, authenticated: !!key });
    } catch (error) { /* Visibility/focus checks cover browsers without channels. */ }

    const originalFetch = window.fetch.bind(window);
    window.fetch = async (input, init) => {
        const request = new Request(input, init);
        const url = new URL(request.url);
        const path = url.pathname.replace(/\/+$/, '').toLowerCase();
        const bound = url.origin === location.origin && (path.startsWith('/api/users')
            || path === '/api/data' || path.startsWith('/api/data/')
            || path.startsWith('/api/settings')
            || path.startsWith('/api/columns')
            || path.startsWith('/api/templates')
            || path.startsWith('/api/backup')
            || ['/api/upload/save-mapping', '/api/upload/saved-mapping'].includes(path)
            || path.startsWith('/api/upload/file/') || path.startsWith('/api/upload/staging/') || path === '/api/import-excel'
            || path.startsWith('/api/clients')
            || ['/api/auth/current-client', '/api/auth/select-client'].includes(path));
        const headers = new Headers(request.headers);
        if (bound && key) headers.set('X-App-Context', key);
        const response = await originalFetch(new Request(request, { headers }));
        if (response.headers.get('X-Context-Changed') === 'true') location.replace('/shell/tenant_select.html');
        return response;
    };

    async function recheckContext(event) {
        if (!key || location.pathname === '/shell/tenant_select.html') return;
        if (event.type === 'pageshow' && !event.persisted) return;
        document.documentElement.style.visibility = 'hidden';
        try {
            const response = await originalFetch('/api/auth/tenants', { cache: 'no-store' });
            if (!response.ok) { location.replace('/static/login.html'); return; }
            const context = await response.json();
            if (context.context_key !== key || String(context.selected_tenant_id || '') !== tenant) {
                location.replace('/shell/tenant_select.html'); return;
            }
            document.documentElement.style.visibility = '';
        } catch (error) {
            // A saved document cannot certify its context while offline.
            location.replace('/shell/tenant_select.html');
        }
    }
    window.addEventListener('pageshow', recheckContext);
    window.addEventListener('focus', recheckContext);
    document.addEventListener('visibilitychange', event => {
        if (document.visibilityState === 'visible') recheckContext(event);
    });
    document.addEventListener('DOMContentLoaded', () => {
        if (!key || location.pathname === '/shell/tenant_select.html') return;
        if (typeof NAV_MANIFEST !== 'undefined') NAV_MANIFEST.push({ title: 'Workspace', items: [
            { label: 'Change workspace', url: '/shell/tenant_select.html', state: 'ready', module: 'Workspace',
                children: [{ label: 'Select business or CA firm', url: '/shell/tenant_select.html', state: 'ready', keywords: ['tenant', 'firm', 'workspace'] }] }
        ] });
        // Extend the existing native navigation registry, as Workspace does;
        // both quick navigation and search continue to read the same manifest.
        if (typeof NAV_MANIFEST !== 'undefined') NAV_MANIFEST.forEach(group => (group.items || []).forEach(item => {
            if (typeof item.url !== 'string') return;
            const path = new URL(item.url, location.href).pathname;
            let child;
            if (path === '/static/templates.html' && window.AppTenantTemplates?.enabled && window.AppTenantTemplates.canEdit)
                child = { label: 'Accounting fields', url: '/static/templates.html?feature=accounting-fields', state: 'ready', keywords: ['amount', 'quantity', 'rate', 'account', 'debit', 'credit', 'column assignments'] };
            if (path === '/static/setup.html' && window.AppConfigurationBackup.enabled && window.AppConfigurationBackup.canEdit)
                child = { label: 'Configuration backup', url: '/static/setup.html?feature=configuration-backup', state: 'ready', keywords: ['export', 'restore', 'configuration', 'settings backup'] };
            if (path === '/static/upload.html' && window.AppNativeUpload.enabled)
                child = { label: 'Upload progress and recovery', url: '/static/upload.html?feature=upload-jobs', state: 'ready', keywords: ['import', 'job', 'progress', 'cancel', 'resume', 'receipt', 'evidence'] };
            if (path === '/static/databank.html' && window.AppNativeDataBank.enabled)
                child = { label: 'Data Bank previews and totals', url: '/static/databank.html?feature=data-reads', state: 'ready', keywords: ['rows', 'footing', 'coverage', 'ledger', 'filter'] };
            if (child) { item.children ||= []; if (!item.children.some(c => c.url === child.url)) item.children.push(child); }
            if (path === '/static/databank.html' && window.AppNativeDataBank.enabled && window.AppNativeDataBank.history) {
                item.children ||= []; const url = '/static/databank.html?feature=history-exports';
                if (!item.children.some(c => c.url === url)) item.children.push({ label: 'Changed/deleted history and downloads', url, state: 'ready', keywords: ['history', 'changed', 'deleted', 'backup', 'export'] });
            }
        }));
        const nav = document.createElement('nav');
        nav.setAttribute('aria-label', 'Workspace');
        const link = document.createElement('a');
        link.href = '/shell/tenant_select.html'; link.textContent = 'Change workspace';
        nav.appendChild(link); document.body.prepend(nav);
    });
})();
