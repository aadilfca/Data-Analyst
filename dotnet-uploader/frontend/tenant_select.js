(() => {
    const list = document.getElementById('workspaceList');
    const status = document.getElementById('workspaceStatus');
    let contextKey = null;
    let busy = false;
    async function load() {
        if (busy) return;
        list.replaceChildren(); status.textContent = 'Loading workspaces…';
        try {
            const response = await fetch('/api/auth/tenants', { cache: 'no-store' });
            if (response.status === 401) { location.replace('/static/login.html'); return; }
            if (!response.ok) throw new Error('unavailable');
            const result = await response.json(); contextKey = result.context_key;
            status.textContent = result.tenants.length ? 'Select a workspace to continue.' : 'No active workspaces are assigned to your account. Contact your administrator.';
            result.tenants.forEach(tenant => {
                const button = document.createElement('button'); button.type = 'button';
                button.textContent = tenant.name + ' — ' + (tenant.type === 'audit_firm' ? 'CA firm' : 'Business');
                button.addEventListener('click', () => select(tenant.id)); list.appendChild(button);
            });
        } catch (error) { status.textContent = 'Could not load workspaces. Use Reload list to try again.'; }
    }
    async function select(id) {
        if (busy) return;
        busy = true; list.querySelectorAll('button').forEach(button => button.disabled = true);
        status.textContent = 'Opening workspace…';
        try {
            const response = await fetch('/api/auth/select-tenant', { method: 'PUT', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ tenantId: id, contextKey }) });
            if (!response.ok) throw new Error('changed');
            location.replace('/static/client_select.html');
        } catch (error) {
            busy = false; await load(); status.textContent = 'Workspace access changed or the request failed. Review the reloaded list and select again.';
        }
    }
    document.getElementById('workspaceRetry').addEventListener('click', load);
    document.getElementById('workspaceLogout').addEventListener('click', async () => {
        try { await fetch('/api/auth/logout', { method: 'POST' }); } catch (error) { /* Login still remains reachable. */ }
        location.replace('/static/login.html');
    });
    load();
})();
