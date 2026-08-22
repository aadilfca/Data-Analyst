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
    loginSubmitBtn.disabled = true;

    try {
        const res = await fetch('/api/auth/login', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ username, password }),
        });

        if (res.ok) {
            window.location.href = '/static/client_select.html';
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
        loginSubmitBtn.disabled = false;
    }
});
