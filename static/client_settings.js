// Client Settings is retained as a direct-bookmark compatibility page only.
// Verification routing is configured centrally in Account Control Centre; this
// page deliberately makes no voucher-prefix API calls and has no editable state.
window.addEventListener('load', function () {
    const status = document.getElementById('settingsStatus');
    if (status) {
        status.textContent = 'Voucher-prefix configuration is retired. Use Account Control Centre for Verification account assignments.';
        status.className = 'upload-status info';
        status.style.display = 'block';
    }
});
