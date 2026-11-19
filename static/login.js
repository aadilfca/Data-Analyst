// ==============================================
// 🔐 LOGIN PAGE
// ==============================================
// Standalone page (no client_context.js/navbar — this runs before any session exists),
// so this file defines its own minimal fetch call rather than relying on client_context.js's
// global API_BASE.

const loginForm = document.getElementById('loginForm');
const loginSubmitBtn = document.getElementById('loginSubmitBtn');
const loginError = document.getElementById('loginError');

loginForm.addEventListener('submit', async (e) => {
    e.preventDefault();

    const username = document.getElementById('usernameInput').value;
    const password = document.getElementById('passwordInput').value;

    loginError.style.display = 'none';
    window.LoadingUI.showSpinner(loginSubmitBtn, 'Logging in');

    try {
        const res = await fetch('/api/auth/login', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ username, password }),
        });

        if (res.ok) {
            try {
                const workspacesResponse = await fetch('/api/auth/tenants', { cache: 'no-store' });
                if (!workspacesResponse.ok) throw new Error('Could not load workspaces');
                const workspaceContext = await workspacesResponse.json();
                const workspaces = workspaceContext.tenants;

                if (!Array.isArray(workspaces) || workspaces.length !== 1 || !workspaceContext.context_key) {
                    window.location.replace('/shell/tenant_select.html');
                    return;
                }

                // A sole workspace is unambiguous; select it through the audited
                // endpoint so login can proceed directly to the client cards.
                const selection = await fetch('/api/auth/select-tenant', {
                    method: 'PUT',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ tenantId: workspaces[0].id, contextKey: workspaceContext.context_key }),
                });
                window.location.replace(selection.ok ? '/static/client_select.html' : '/shell/tenant_select.html');
            } catch (err) {
                // Preserve the existing landing path if workspace lookup is unavailable;
                // the C# shell will send an unselected session to its workspace picker.
                window.location.replace('/static/client_select.html');
            }
            return;
        }

        // 401 (bad credentials) and 403 (inactive account) both render the same
        // generic message, matching the backend's deliberate same-message behaviour.
        loginError.textContent = 'Invalid username or password.';
        loginError.style.display = 'block';
    } catch (err) {
        loginError.textContent = 'Could not reach the server. Please try again.';
        loginError.style.display = 'block';
    } finally {
        window.LoadingUI.hideSpinner(loginSubmitBtn);
    }
});
