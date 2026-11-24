// ==============================================
// 🌐 GLOBAL CLIENT CONTEXT – Centralised Switching
// ==============================================

// Relative on purpose: the C# frontend and API are served from the same origin in development and production.
const API_BASE = '';

// Session-backed selected-client cache (replaces the old localStorage handoff). Populated once
// per page load by loadSessionClient() (called from initClientContext() before anything renders
// that depends on it) via GET /api/auth/current-client. Every getter below reads this in-memory
// object rather than localStorage, keeping the existing synchronous getter interface every
// dependent page already relies on.
let sessionClientCache = { id: null, name: null, code: null };

// Set true by loadSessionClient() only when BOTH its attempts to reach GET /api/auth/current-client
// fail (network error / timeout / 5xx) — i.e. a transient reachability problem, NOT a valid
// "no client selected" response. enforceClientSelection() reads this to tell the two apart:
// before this flag existed, a failed/slow call on a cold Render instance nulled the cache and was
// treated as "no client", evicting a user who genuinely had a client selected (confirmed
// cold-start bug, 2026-08-27).
let sessionClientLoadFailed = false;

// Ready signal for page scripts. initClientContext() is async (it awaits a real network fetch),
// so window.onload is NOT a safe barrier for it — on a light page, onload fires long before the
// fetch resolves, and any page reading getSelectedClientId() at that moment sees null and wrongly
// concludes no client is selected. Every page's init must await this promise before reading the
// client. Created here at parse time (not inside initClientContext()) so it always exists by the
// time a page script's own onload handler runs, since client_context.js is loaded first everywhere.
// Resolves once the session/navbar are ready, and never rejects — loadSessionClient() swallows
// its own network/parse errors and leaves the cache null (setting sessionClientLoadFailed so the
// guard shows a "reload" prompt rather than evicting), so a rejection here would hang every page
// instead of letting it render. Exception: on a 401 (invalid/expired
// session), initClientContext() returns before ever calling resolveClientContextReady() (see
// its own early-return comment) — this promise is then deliberately left permanently unresolved,
// since the page is already navigating away to login.html. Resolving it here instead would wake
// up every page's `await window.clientContextReady` (verification.js, databank.js,
// trial_balance.js) mid-navigation with a null client cache, reintroducing the exact spurious
// "please select a client" alert this was built to prevent (confirmed live, 2026-08-22).
let resolveClientContextReady;
window.clientContextReady = new Promise(function (resolve) {
    resolveClientContextReady = resolve;
});

// Returns true if the session is valid OR a transient network/parse error occurred (the caller
// must still render *something*), false only on a 401. The caller (initClientContext()) checks
// this and bails on false - a 401 already schedules a redirect to login.html here, but that alone
// doesn't stop the caller from rendering navbar/client-control state against a null cache, which
// used to fire a spurious "please select a client" alert and race a second redirect to
// index.html against this function's own login.html redirect (confirmed live, 2026-08-22).
//
// Retry: a cold Render instance often fails or times out the very first request while it spins
// up, then serves the next one fine. So a failed attempt (network error OR !res.ok) is retried
// once after a short delay before giving up. A 401 is never retried - the server did respond, the
// session is genuinely invalid. On both attempts failing, sessionClientLoadFailed is set so
// enforceClientSelection() shows a "reload" prompt instead of evicting the user as "no client
// selected" (the cold-start bug this fixes).
async function loadSessionClient() {
    // Firm-level pages are authenticated and tenant-role-checked before the
    // server sends their HTML. They do not need a selected-client lookup.
    if (isFirmLevelPage()) return true;
    sessionClientLoadFailed = false;
    for (let attempt = 0; attempt < 2; attempt++) {
        try {
            const res = await fetch(API_BASE + '/api/auth/current-client');
            if (res.status === 401) {
                // No valid session - bounce to login rather than rendering a page the
                // user isn't allowed to see. Not retried (see above).
                window.location.href = '/static/login.html';
                return false;
            }
            if (!res.ok) throw new Error('HTTP ' + res.status);
            const data = await res.json();
            sessionClientCache = {
                id: data.client_id || null,
                name: data.client_name || null,
                code: data.client_code || null,
            };
            return true;
        } catch (error) {
            console.warn(`⚠️ current-client attempt ${attempt + 1} failed:`, error.message);
            if (attempt === 0) {
                // Give a cold instance a moment to finish starting, then try once more.
                await new Promise(resolve => setTimeout(resolve, 1500));
                continue;
            }
            // Both attempts failed - transient reachability problem, NOT "no client selected".
            sessionClientLoadFailed = true;
            sessionClientCache = { id: null, name: null, code: null };
            return true;
        }
    }
}

// ---------- NAVIGATION LOADING FEEDBACK ----------
// One token covers the current navigation transition. It blocks duplicate navigation clicks only;
// unrelated page controls remain usable while the browser or an async guard is completing the move.
let navigationLoadingToken = null;

function startNavigationLoading(target) {
    if (navigationLoadingToken) return false;
    if (window.LoadingUI) navigationLoadingToken = window.LoadingUI.startGlobalLoading(target);
    return true;
}

function stopNavigationLoading() {
    if (!navigationLoadingToken) return;
    if (window.LoadingUI) window.LoadingUI.stopGlobalLoading(navigationLoadingToken);
    navigationLoadingToken = null;
}

// A real page transition replaces this document, but Escape/Stop or bfcache can leave it alive.
// These event-driven releases avoid a guessed timeout and prevent a stranded cursor.
window.addEventListener('pagehide', stopNavigationLoading);
window.addEventListener('pageshow', stopNavigationLoading);
document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape') stopNavigationLoading();
});

/**
 * Log out: clear the session server-side, then redirect to login regardless of
 * response (logout is designed to always succeed / be idempotent).
 */
async function logoutUser(target) {
    if (!startNavigationLoading(target)) return;
    try {
        await fetch(API_BASE + '/api/auth/logout', { method: 'POST' });
    } catch (error) {
        console.warn('⚠️ Logout request failed:', error.message);
    }
    window.location.href = '/static/login.html';
}

// Navbar greeting. Was hardcoded in all 15 pages' HTML and had drifted into two variants
// ("Admin" on 8 pages, "Analyst" on 7) — a meaningless split, since users.role is unused and
// there is one seeded account. Standardised here as the single source. When a future
// roles/permissions module renders a real username, this constant is the one place to change:
// GET /api/auth/current-client DOES now return `username` (added for client_select.html's header,
// which renders the real name), so switching this greeting to the live value is a small change —
// it just needs the app-wide cache-bump that touching this file entails.
const NAV_USER_LABEL = '👤 Welcome, Analyst';

// ---------- GLOBAL QUICK-NAVIGATION MANIFEST ----------
// Hand-maintained tree describing every navigable destination, grouped exactly like the
// dashboard's three sections (static/index.html). Not a live scrape of the DOM — whoever
// edits either side must mirror changes into the other manually. Plain data (no rendering
// logic) so the search field can flatten/filter this same array directly.
// `module` (when present) is matched against window._appNav.module to highlight the
// current page as "you are here" (see getUpTarget()/data-nav-module below).
// `children` mirror a module's own internal card/hub grid or in-page feature list one level
// deep (no further nesting supported by renderNavMenuPanel()/renderNavSearchResults()) — used
// both for real sub-pages (Verification's Voucher/Process verification tiles, Setup's own
// 5-card grid) and, since the Phase D nav-data merge, for a page's ground-level in-page
// features that have a real visible entry point but no page of their own (Audit Rules' rail
// entities + Assign Categories/Signature Specimens, deep-linked via `?feature=<slug>` — see
// "Global Header Nav Rebuild" in CHANGELOG_ARCHIVE.md for the merge history; this replaces the
// old standalone FEATURE_SEARCH_INDEX/static/search_index.js, deleted in that same phase). A
// child MAY carry `keywords: string[]` (lowercase synonym phrases) — flattenNavManifest()
// carries them through and renderNavSearchResults() matches them by substring alongside label,
// exactly like FEATURE_SEARCH_INDEX's old matching did; a child with no keywords is matched by
// label only, same as before this phase.
// Verification's Cash vouchers/Bank vouchers children use the same ?type=cash/?type=bank URLs as
// verification.html's own hub tiles (#cashVoucherTile/#bankVoucherTile in verification.js) — an
// earlier session added `?view=cashbook`/`?view=bankbook` as separate "Cashbook"/"Bank book"
// entries before realising both resolve to the exact same currentType/openSessionPopup() call as
// ?type=; consolidated down to one entry per destination, matching the hub's own wording exactly.
// (window.onload in verification.js still recognises ?view= too — harmless unused code path now
// that nothing links to it, left as-is since removing it isn't needed for this manifest change.)
const NAV_MANIFEST = [
    {
        title: 'Audit work',
        items: [
            {
                label: 'Verification', url: '/static/verification.html', state: 'ready', module: 'Verification',
                children: [
                    { label: 'Cash vouchers', url: '/static/verification.html?type=cash', state: 'ready' },
                    { label: 'Bank vouchers', url: '/static/verification.html?type=bank', state: 'ready' },
                    { label: 'Local vendor', url: '/static/verification.html?type=local_vendor', state: 'ready' },
                    { label: 'Import vendor', url: '/static/verification.html?type=import_vendor', state: 'ready' },
                    { label: 'Export customer', url: '/static/verification.html?type=export_customer', state: 'ready' },
                    { label: 'Payroll', url: '/static/verification.html?type=payroll', state: 'ready' },
                    { label: 'Contractor', url: '/static/verification.html?type=contractor', state: 'ready' },
                    { label: 'Store receiving vs vendor JV', url: null, state: 'coming-soon' },
                    { label: 'PO vs gate receiving vs store receiving', url: null, state: 'coming-soon' },
                    { label: 'Store issuance vs article manufactured', url: null, state: 'coming-soon' },
                ]
            },
            { label: 'Reports', url: '/static/reconciliation.html?view=reports', state: 'ready', module: 'Reports',
                children: [{ label: 'Customer and vendor reversals', url: '/static/reconciliation.html?report=cashflows', state: 'ready',
                    keywords: ['customer receipts', 'vendor payments', 'refunds', 'reversals', 'period-wise', 'net cashflow'] },
                    { label: 'Ledger / Cashflow reconciliation', url: '/static/reconciliation.html?report=cashflows&feature=reconciliation', state: 'ready',
                        keywords: ['cash bank ledger', 'receipts payments', 'adjustments', 'voucher differences', 'reconciliation'] },
                    { label: 'Financial Statements', url: '/static/reconciliation.html?report=financial-statements', state: 'ready',
                        keywords: ['financial accounts', 'statement outline'] },
                    { label: 'Balance Sheet', url: '/static/reconciliation.html?report=balance-sheet', state: 'ready',
                        keywords: ['financial position', 'assets', 'liabilities', 'equity', 'financial statements'] },
                    { label: 'Balance Sheet comparative', url: '/static/reconciliation.html?report=balance-sheet', state: 'ready',
                        keywords: ['opening balances', 'comparative column', 'opening income statement items', 'opening reconciliation'] },
                    { label: 'Income Statement', url: '/static/reconciliation.html?report=income-statement', state: 'ready',
                        keywords: ['profit and loss', 'sales', 'expenses', 'profit after taxation', 'financial statements'] },
                    { label: 'Balance Sheet reconciliation', url: '/static/reconciliation.html?report=balance-sheet&feature=tb-reconciliation', state: 'ready',
                        keywords: ['trial balance completeness', 'footing', 'accounting equation', 'assets capital liabilities', 'unexplained difference'] }] },
            { label: 'Analysis', url: null, state: 'coming-soon' },
        ],
    },
    {
        title: 'Client Data',
        items: [
            { label: 'Upload Client Data', url: '/static/upload.html', state: 'ready', module: 'Upload Data',
                children: [{ label: 'Account Ledger upload', url: '/static/upload.html?feature=account-ledger', state: 'ready',
                    keywords: ['ledger', 'cash bank ledger', 'opening closing balances'] }] },
            { label: 'Client Data Bank', url: '/static/databank.html', state: 'ready', module: 'Data Bank',
                children: [
                    { label: 'Database safeguards', url: '/static/data_integrity.html?feature=database-safeguards', state: 'ready',
                        keywords: ['data integrity', 'indexes', 'index health', 'database safeguards', 'duplicate protection'] },
                    { label: 'Module inventory', url: '/static/data_integrity.html?feature=module-inventory', state: 'ready',
                        keywords: ['data integrity', 'record counts', 'data size', 'duplicates', 'stored data'] },
                ] },
            { label: 'Trial Balance', url: '/static/trial_balance.html', state: 'ready', module: 'Trial Balance',
                children: ['financial', 'cashflow', 'verification'].map(function (mode) {
                    const label = {financial: 'Financial Statement assignments', cashflow: 'Cashflow assignments', verification: 'Verification assignments'}[mode];
                    return {label: label, url: '/static/trial_balance.html?view=available&workflow=' + mode, state: 'ready', keywords: ['trial balance', 'account assignment', mode, 'category', 'notes', 'correct account codes', 'batch corrections']};
                }) },
        ],
    },
    {
        title: 'Administration',
        // Manage Clients (ready) and the Settings/Users coming-soon placeholders were removed
        // here (Admin area, Phase 8 of 8, FINAL) - that whole area is now reached exclusively via
        // the admin-only gear icon on client_select.html, not from inside a client's own
        // workspace. Setup's own "App Settings" child link below is untouched - pre-existing
        // Setup-hub navigation, unrelated to this removal (confirmed with the user).
        items: [
            {
                label: 'Setup', url: '/static/setup.html', state: 'ready', module: 'Setup',
                children: [
                    { label: 'Manage Columns', url: '/static/columns.html', state: 'ready' },
                    { label: 'Manage Templates', url: '/static/templates.html', state: 'ready' },
                    { label: 'Error Dashboard', url: '/static/error_dashboard.html', state: 'ready' },
                    { label: 'App Settings', url: '/static/settings.html', state: 'ready' },
                    { label: 'Storage Cleanup', url: '/static/storage_cleanup.html', state: 'ready', keywords: ['database size', 'storage', 'cleanup', 'maintenance'] },
                ]
            },
            {
                label: 'Audit Rules', url: '/static/audit_rules.html', state: 'ready', module: 'Audit Rules',
                children: [
                    {
                        label: 'Documents', url: '/static/audit_rules.html?feature=documents', state: 'ready',
                        keywords: ['supporting document', 'audit document', 'risk rating', 'authorisation roles'],
                    },
                    {
                        label: 'Cross-checks', url: '/static/audit_rules.html?feature=cross-checks', state: 'ready',
                        keywords: ['cross check', 'crosscheck', 'compare field', 'comparison', 'document chain'],
                    },
                    {
                        label: 'Categories', url: '/static/audit_rules.html?feature=categories', state: 'ready',
                        keywords: ['category', 'transaction type', 'validation rules', 'cleared requirements'],
                    },
                    {
                        label: 'Voucher-level Checks', url: '/static/audit_rules.html?feature=voucher-checks', state: 'ready',
                        keywords: ['voucher check', 'prepared by', 'checked by', 'approved by'],
                    },
                    {
                        label: 'Assign Categories', url: '/static/audit_rules.html?feature=assign-categories', state: 'ready',
                        keywords: ['assign category', 'account category', 'trial balance accounts', 'account head'],
                    },
                    {
                        label: 'Signature Specimens', url: '/static/audit_rules.html?feature=signature-specimens', state: 'ready',
                        keywords: ['signature specimen', 'specimen', 'signatory', 'authority'],
                    },
                    {
                        // Assertions Phase 1 of 5: uses the real ?entity= param audit_rules.js's
                        // restoreAuditRulesViewFromUrl() actually reads (AR_VALID_ENTITY_TYPES) -
                        // unlike the sibling entries above, which still use the dead ?feature=
                        // param (pre-existing, out of scope for this phase; audit_rules.js never
                        // reads it, confirmed via grep).
                        label: 'Assertions', url: '/static/audit_rules.html?entity=assertions', state: 'ready',
                        keywords: ['assertion', 'existence', 'occurrence', 'completeness', 'accuracy', 'valuation', 'cut-off', 'authorisation', 'coverage'],
                    },
                ]
            },
            { label: 'Audit Log', url: null, state: 'coming-soon' },
        ],
    },
];

/**
 * A manifest item's `url` may be either a plain string (the usual case) or a function returning one,
 * for a destination that can only be resolved at render time — currently just Client Settings, whose
 * target needs the session's ?client_id=. Called at both places an href is built from an item
 * (renderNavMenuPanel() and renderNavSearchResults()), so a function-valued url is resolved in
 * exactly one place per surface. Every existing string url passes through untouched.
 */
function resolveNavItemUrl(item) {
    return typeof item.url === 'function' ? item.url() : item.url;
}

/**
 * Flatten NAV_MANIFEST into a single ordered array for search filtering. Pure data
 * transform (no DOM) — recomputed fresh on every keystroke since the manifest is tiny.
 * Each entry carries groupTitle/isChild so renderNavSearchResults() can re-group matches
 * in the manifest's own order without a second, drift-prone copy of the group structure.
 * `keywords` is carried through for top-level items as well as children — until Client Settings
 * only children ever declared any, so a top-level item's keywords were silently dropped here and
 * the entry stayed findable by label alone. Harmless for every item that declares none (empty
 * array, matches nothing extra).
 */
function flattenNavManifest() {
    const flat = [];
    NAV_MANIFEST.forEach(group => {
        group.items.forEach(item => {
            flat.push({ label: item.label, url: item.url, state: item.state, groupTitle: group.title, isChild: false, keywords: item.keywords || [] });
            (item.children || []).forEach(child => {
                flat.push({ label: child.label, url: child.url, state: child.state, groupTitle: group.title, isChild: true, keywords: child.keywords || [] });
            });
        });
    });
    return flat;
}

// The dashboard's "🏢 Client" <select> and everything that served it (clientList/
// selectedClientId/clientSelectElement state, getClientSelect(), loadClientDropdown(),
// updateSelectedClient()) were removed once client selection moved onto the session:
// static/client_select.html is now the only place a client is picked, and it writes the
// choice via its own PUT /api/auth/select-client. Keeping a second, dashboard-only picker
// here meant two competing ways to change the same session value. The navbar now shows a
// fixed read-only display instead — see renderClientControl() below.

// ---------- UI UPDATES (for upload page) ----------
function updateClientDependentUI() {
    const hasClient = !!getSelectedClientId();
    const uploadBtn = document.getElementById('importBtn');
    const warningEl = document.getElementById('clientSelectionWarning');

    if (warningEl) {
        warningEl.style.display = hasClient ? 'none' : 'flex';
    }
    if (uploadBtn) {
        const templateSelected = document.getElementById('templateDropdown') &&
            document.getElementById('templateDropdown').value;
        uploadBtn.disabled = !hasClient || !templateSelected;
    }
}

// ---------- GETTERS (session-backed via sessionClientCache, see loadSessionClient() above) ----------
function getSelectedClientId() {
    return sessionClientCache.id;
}

function getSelectedClientName() {
    return sessionClientCache.name;
}

function getSelectedClientCode() {
    return sessionClientCache.code;
}

/**
 * Single source of truth for "where does Client Settings go?", shared by the dashboard's
 * #clientSettingsCard (script.js) and this file's own NAV_MANIFEST entry.
 *
 * client_settings.js's loadClientSettings() reads ?client_id=N straight off the URL and hard-errors
 * without one, so this page can never be reached by a static link from a client-scoped page — the id
 * has to come from the session at click time. With no client selected we send the user to the picker
 * rather than to a page that would only show "No client_id was provided".
 *
 * Deliberately synchronous: every caller resolves this on a real click/render, long after
 * loadSessionClient() has populated the cache, so there's no clientContextReady race to await here —
 * and if one somehow read it early, the null branch degrades to the picker rather than breaking.
 *
 * `&from=dashboard` stamps this as the client-scoped door (dashboard card + ⊞ nav entry, both of
 * which resolve through here), so client_settings.html's Up arrow and back-link return to the
 * dashboard rather than to the Admin-area clients.html the page used to hardcode. The Admin door
 * stamps its own `&from=clients` at its own call site in clients.js — see NAV_ENTRY_SOURCES.
 *
 * The no-client branch carries `?next=client_settings` so the picker can return the user to Client
 * Settings once they choose a client, instead of dropping them on the dashboard and silently
 * discarding what they actually asked for. Only this bare key is written — never a path or URL:
 * client_select.js resolves it through its own NEXT_DESTINATIONS whitelist, for the same
 * open-redirect reason NAV_ENTRY_SOURCES is a lookup table. That whitelist lives there rather than
 * here because client_select.html is a standalone pre-session page that cannot load this file at all.
 */
function getClientSettingsUrl() {
    const clientId = getSelectedClientId();
    return clientId
        ? '/static/client_settings.html?client_id=' + clientId + '&from=dashboard'
        : '/static/client_select.html?next=client_settings';
}
window.getClientSettingsUrl = getClientSettingsUrl;

function isClientSelected() {
    return !!getSelectedClientId();
}

// ---------- PAGE DETECTION ----------
function isDashboardPage() {
    const path = window.location.pathname;
    return path.endsWith('index.html') || path === '/' || path === '/static/' || path === '/static/index.html';
}

// Firm-level pages (Admin area, Phase 6 of 8) - not client-scoped, so they shouldn't need a
// client selected to load. A named exemption list, not a general opt-out mechanism - only these
// three pages are exempt. The admin-role guards on top of this are untouched and still apply -
// clients.js's checkAdminAccess(), users.js's own 403-aware loadUsers(), the Storage Cleanup
// route's server-side guard, and the tab strip's admin-only gating all remain layered together.
const FIRM_LEVEL_PAGES = ['settings.html', 'users.html', 'clients.html', 'storage_cleanup.html'];

function isFirmLevelPage() {
    const currentFile = window.location.pathname.split('/').pop() || '';
    return FIRM_LEVEL_PAGES.includes(currentFile);
}

// True only for client_settings.html opened via the Admin door (clients.html's cogwheel).
// Deliberately NOT added to FIRM_LEVEL_PAGES: that list applies unconditionally, and this page
// must keep its normal client-scoped chrome on the other door (?from=dashboard).
function isClientSettingsAdminDoor() {
    const currentFile = window.location.pathname.split('/').pop() || '';
    if (currentFile !== 'client_settings.html') return false;
    const params = new URLSearchParams(window.location.search);
    return params.get('from') === 'clients';
}

// Pages that read their own client from ?client_id= in the URL instead of from the session.
// A named lookup list, NOT a general opt-out — same convention as FIRM_LEVEL_PAGES above and
// NAV_ENTRY_SOURCES below. Only add a page here if its OWN loader actually reads ?client_id=
// off the URL; every other client-scoped page (databank/verification/upload/...) resolves its
// client from getSelectedClientId(), so exempting one of those on the strength of a URL param
// would let it load with a null client and render a broken page — replacing a clear message
// with silent breakage.
const URL_CLIENT_SCOPED_PAGES = ['client_settings.html'];

// Parses ?client_id= using the SAME validation client_settings.js's loadClientSettings() applies,
// deliberately duplicated in shape so the guard and the loader can never disagree about what
// counts as a valid id (a value this accepts but the loader rejects would let a page through the
// guard only to hard-error on arrival). Returns the integer, or null when absent/non-numeric.
function getUrlClientId() {
    const params = new URLSearchParams(window.location.search);
    const idParam = params.get('client_id');
    if (!idParam || isNaN(parseInt(idParam, 10))) return null;
    return parseInt(idParam, 10);
}

// True only when BOTH hold: this page is one that reads its client from the URL, and the URL
// actually carries a valid id. Both conditions matter — a stray ?client_id= on any other page
// grants nothing.
function hasUrlSuppliedClient() {
    const currentFile = window.location.pathname.split('/').pop() || '';
    return URL_CLIENT_SCOPED_PAGES.includes(currentFile) && getUrlClientId() !== null;
}

// Mirrors sessionClientCache in shape, so the pill's data source stays a simple synchronous read.
// Populated (or left null) by loadUrlClient() below; getUrlClientName() is its only reader.
let urlClientCache = { id: null, name: null };

// Resolves the URL-supplied client's NAME for the navbar pill on a page in URL_CLIENT_SCOPED_PAGES.
// A no-op everywhere else. Called once from initClientContext(), right after loadSessionClient() -
// that call is already async and already awaits a real network fetch, so this adds no new timing
// model and no separate clientContextReady race.
//
// On the dashboard door the URL id and the session id are always the same value (getClientSettingsUrl()
// stamps the session's own selected client), so this skips the extra fetch and just copies the
// session's already-loaded name. Only the Admin door (a URL id with no matching session client) ever
// needs a real GET /api/clients/{id}.
//
// Errors are swallowed and leave the cache null, matching loadSessionClient()'s own warn-only
// convention - a failed lookup degrades to today's "Client: None selected" pill, never a broken navbar.
async function loadUrlClient() {
    if (!hasUrlSuppliedClient()) return;

    const urlId = getUrlClientId();
    if (urlId === getSelectedClientId()) {
        urlClientCache = { id: urlId, name: getSelectedClientName() };
        return;
    }

    try {
        const res = await fetch(API_BASE + '/api/clients/' + urlId);
        if (!res.ok) throw new Error('Failed to load URL-supplied client');
        const client = await res.json();
        urlClientCache = { id: urlId, name: client.client_name || null };
    } catch (error) {
        console.warn('⚠️ Failed to load URL-supplied client:', error.message);
        urlClientCache = { id: null, name: null };
    }
}

function getUrlClientName() {
    return urlClientCache.name;
}

// When enforceClientSelection() evicts a user from a client-scoped page because no client is
// selected, it sends them to the Client Cards picker with a ?next=<key> stamp so they land back on
// the page they actually asked for after choosing — the same mechanism getClientSettingsUrl()'s
// no-client branch already uses, generalised to the eviction path.
// SECURITY: whitelist lookup, exactly like NAV_ENTRY_SOURCES. This returns a KEY, never a path —
// client_select.js resolves the key through its own NEXT_DESTINATIONS table. An unrecognised page
// returns null and the caller falls back to a plain client_select.html (still better than the old
// index.html, which can't fix a missing client anyway).
function getEvictionNextKey() {
    const currentFile = window.location.pathname.split('/').pop() || '';
    if (currentFile === 'verification.html') {
        // Cash vs Bank is carried in ?type= on this page (verification.js reads it the same way).
        const type = new URLSearchParams(window.location.search).get('type');
        if (type === 'bank') return 'verification_bank';
        if (type === 'cash') return 'verification_cash';
    }
    return null;
}

// ---------- ENFORCE CLIENT SELECTION ----------
function enforceClientSelection() {
    // Native error administration is workspace-wide and server-guarded for administrators.
    if (window.AppNativeAdministration && window.location.pathname === '/static/error_dashboard.html') return true;
    if (!isClientSelected()) {
        // A page that carries its own valid ?client_id= already has everything it needs, so
        // blocking it here was wrong: the clients.html cogwheel builds a fully-resolved
        // client_settings.html?client_id=N&from=clients, and this guard evicted it anyway
        // because the Admin flow (picker → gear → Settings → Manage Clients) never sets a
        // session client. See hasUrlSuppliedClient() above for why that exemption is a named
        // whitelist rather than "any page with a client_id param".
        if (hasUrlSuppliedClient()) return true;

        // Only enforce on non-dashboard, non-firm-level pages
        if (!isDashboardPage() && !isFirmLevelPage()) {
            if (sessionClientLoadFailed) {
                // Couldn't REACH the server to load the selected client — not the same as "no
                // client selected". Evicting here was the cold-start bug: the client may well be
                // selected. Tell the user to reload (a reload usually succeeds once the instance
                // is warm) and do NOT redirect.
                alert("⚠️ Couldn't reach the server to load your selected client. This can happen "
                    + "on the first request after a period of inactivity — please reload the page.");
                return false;
            }
            // Genuinely no client selected: send the user to the Client Cards picker (not the
            // dashboard — the dashboard can't fix a missing client), preserving where they were
            // headed via a whitelisted ?next= key so they return there after choosing.
            alert('⚠️ Please select a client from the Client Cards page first.');
            const nextKey = getEvictionNextKey();
            window.location.href = nextKey
                ? '/static/client_select.html?next=' + nextKey
                : '/static/client_select.html';
            return false;
        }
        return false;
    }
    return true;
}

// ---------- RENDER CLIENT CONTROL (fixed read-only client display) ----------
// One display on every protected page, dashboard included — the old dashboard-only dropdown
// is gone (see the note above updateClientDependentUI()). The pill's own styling now lives in
// style.css as .nav-client-display rather than the inline style="..." this markup used to carry.
// AREA/stats remain non-dashboard-only and keep their existing inline styles untouched.
// Safe to render synchronously: initClientContext() awaits loadSessionClient() before calling
// this, so sessionClientCache is already populated — no window.onload dependency anywhere here.
function renderClientControl() {
    const container = document.getElementById('clientControl');
    if (!container) return;

    // A session can legitimately have no client yet (GET /api/auth/current-client returns nulls
    // when selected_client_id is unset), so this renders an explicit state rather than blank.
    // Both states are clickable and both lead to client_select.html — see the delegated
    // .nav-client-display listener at the bottom of this file for how the two differ (the
    // no-client state skips the confirm, since there's nothing to lose).
    //
    // getUrlClientName() takes priority when present: on a page in URL_CLIENT_SCOPED_PAGES reached
    // with no session client (the Admin door), the URL's client is the one actually on screen, so a
    // pill showing "None selected" would contradict the page heading - and worse, render the
    // client-none variant, whose click handler skips the confirm dialog and jumps straight to the
    // picker. Everywhere else this is null and the pill behaves exactly as before.
    const clientName = getUrlClientName() || getSelectedClientName();
    const clientDisplay = clientName
        ? `<button type="button" class="nav-client-display" title="Switch client">Client: <strong>${clientName}</strong></button>`
        : `<button type="button" class="nav-client-display client-none" title="Select a client">Client: None selected</button>`;

    if (isDashboardPage()) {
        container.innerHTML = clientDisplay;
    } else {
        const moduleName = window._appNav?.module || 'Dashboard';
        const statsText = window._appNav?.stats || '';

        container.innerHTML = `
            ${clientDisplay}
            <span class="nav-area" style="font-weight:600; font-size:14px; color:#e0f2fe; letter-spacing:0.3px; white-space:nowrap;">AREA: ${moduleName}</span>
            <span id="navStats" style="font-size:13px; color:#94a3b8; white-space:nowrap;">${statsText}</span>
        `;
        // Enforce client selection on all non-dashboard pages, except the firm-level
        // pages exempted above (Admin area, Phase 6 of 8)
        enforceClientSelection();
    }
}

// ---------- GLOBAL NAVBAR UPDATE FUNCTIONS ----------
// backOverride: LEGACY, pending removal in Phase B. No longer consulted by the Back-arrow click
// handler (Back is now the chronological history stack below) — kept defined only so existing
// callers (e.g. verification.js's setBackOverride(exitSession)) don't throw. Its intended meaning
// ("go to a sensible parent view") is superseded by upOverride, which Phase B will migrate callers
// onto.
// upOverride: null (default, static getUpTarget() resolution) or a function invoked instead of
// navigating — for single-page/multi-view modules (e.g. Verification) whose "up" destination
// depends on in-page view state rather than a fixed URL.
// inPageRestoreHandler: null, or a function(viewKey) a multi-view page registers so the Back
// history stack can restore one of its in-page views instead of navigating (see
// setInPageRestoreHandler()/pushInPageNavEntry() below).
window._appNav = window._appNav || { module: 'Dashboard', stats: '', backOverride: null, upOverride: null, inPageRestoreHandler: null };

/**
 * Set the module name displayed in the navbar (e.g., "Verification", "Upload")
 * @param {string} name - The module name (e.g., "Verification")
 */
window.setNavModule = function(name) {
    window._appNav.module = name || 'Dashboard';
    // Re-render the entire navbar to reflect the change
    renderClientControl();
};

/**
 * Set the statistics text displayed in the navbar (e.g., "Session 14 · Cashbook: ...")
 * @param {string} stats - The stats text to display (empty string to hide)
 */
window.setNavStats = function(stats) {
    window._appNav.stats = stats || '';
    // Re-render the entire navbar to reflect the change
    renderClientControl();
};

/**
 * Update the client name displayed in the navbar.
 * Its only caller was the removed updateSelectedClient(), so nothing invokes this today — the
 * client can no longer change without a page navigation, which re-renders the display anyway.
 * Kept (and repointed at the renamed .nav-client-display element rather than the old
 * .nav-client-name) only because it is a window.* export; flagged for removal in Phase 4.
 * @param {string} name - The client name (e.g., "ACME")
 */
window.updateNavClientName = function(name) {
    const clientSpan = document.querySelector('.nav-client-display');
    if (clientSpan) {
        clientSpan.innerHTML = name
            ? `Client: <strong>${name}</strong>`
            : 'Client: None selected';
    }
};

/**
 * LEGACY, pending removal in Phase B — no longer consulted by the Back-arrow click handler.
 * Kept only so existing callers (verification.js) don't throw. Use setUpOverride() instead.
 * @param {Function|null} fn
 */
window.setBackOverride = function(fn) {
    window._appNav.backOverride = (typeof fn === 'function') ? fn : null;
};

/**
 * Override the Up arrow's behavior for pages with in-page view state (e.g. Verification's
 * Cashbook-selection vs active-session views), where "up" isn't a fixed URL.
 * Pass a function to intercept Up-arrow clicks (called instead of navigating); pass null
 * (or omit) to restore the page's static data-nav-parent/NAV_PARENT_MAP/index.html default.
 * @param {Function|null} fn
 */
window.setUpOverride = function(fn) {
    window._appNav.upOverride = (typeof fn === 'function') ? fn : null;
};

/**
 * Register a handler the Back history stack calls to restore one of this page's in-page views
 * (e.g. Verification's cashbook/session views) instead of navigating, when the stack's target
 * entry shares this page's URL but carries a viewKey. Pass null to unregister.
 * @param {Function|null} fn - called as fn(viewKey)
 */
window.setInPageRestoreHandler = function(fn) {
    window._appNav.inPageRestoreHandler = (typeof fn === 'function') ? fn : null;
};

// ---------- UP-ARROW PARENT-TARGET MECHANISM ----------
// Page-filename -> parent-filename config map. Left empty in this foundation phase;
// later phases populate real entries as each page is wired (e.g. 'client_settings.html': 'clients.html').
const NAV_PARENT_MAP = {
    // 'somepage.html': 'parentpage.html',
};

// ---------- ENTRY-POINT ("?from=") SOURCES ----------
// A page reachable through more than one door can't have a single correct static parent. Client
// Settings is the first such page: the clients.html cogwheel (Admin area) and the dashboard card /
// ⊞ nav entry are both legitimate ways in, and its old hardcoded data-nav-parent="clients.html"
// sent every dashboard visitor back to an admin-gated page they may not even have rights to.
// Each door stamps its own identity onto the URL (&from=<key>) and this table turns that key into a
// destination + a human label for the header's own back-link.
// Chosen over inferring the door from the Back history stack: the param is part of the URL, so it
// survives a refresh, a restored/duplicated tab and a pasted link, none of which sessionStorage
// does — and the stack deliberately excludes firm-level pages, so it structurally cannot see
// clients.html, the very door that most needs distinguishing.
// SECURITY: this is a whitelist lookup on purpose. Never build a path by concatenating the raw
// param ('/static/' + fromParam) — that is an open redirect. An unknown/absent key simply falls
// through to the normal resolution chain below.
const NAV_ENTRY_SOURCES = {
    clients: { file: 'clients.html', label: 'Manage Clients' },
    dashboard: { file: 'index.html', label: 'Dashboard' },
};

/**
 * Resolve the current page's entry point from ?from=, or null when it's absent/unrecognised.
 * Shared by getUpTarget() and renderUpLinks() so the Up arrow and the in-page back-link can never
 * disagree about where "back" is — drifting apart is exactly how client_settings.html ended up with
 * an arrow and a link pointing at two different ideas of its parent.
 */
function getNavEntrySource() {
    const params = new URLSearchParams(window.location.search);
    const from = params.get('from');
    if (!from) return null;
    return NAV_ENTRY_SOURCES[from] || null;
}

/**
 * Resolve the Up arrow's destination for the current page.
 * Priority: ?from= entry point, then <body data-nav-parent="..."> override, then NAV_PARENT_MAP,
 * then index.html. The three original steps are unchanged — a page with no ?from= resolves exactly
 * as it did before this existed.
 */
function getUpTarget() {
    const entrySource = getNavEntrySource();
    if (entrySource) return '/static/' + entrySource.file;

    const override = document.body.getAttribute('data-nav-parent');
    if (override) return '/static/' + override;

    const currentFile = window.location.pathname.split('/').pop() || 'index.html';
    const mapped = NAV_PARENT_MAP[currentFile];
    if (mapped) return '/static/' + mapped;

    return '/static/index.html';
}

/**
 * Point any in-page back-link marked `data-nav-up-link` at the same destination the Up arrow would
 * use, relabelling it to match ("← Back to Manage Clients" / "← Back to Dashboard").
 * Opt-in per anchor, so no existing .back-link anywhere else in the app is affected. Called from
 * initClientContext()'s client-scoped branch; a page with no such anchor is a no-op.
 * With no recognised ?from=, the label follows getUpTarget()'s own fallback — index.html — rather
 * than whatever text the HTML happened to ship with, keeping link and arrow in agreement.
 */
function renderUpLinks() {
    const links = document.querySelectorAll('[data-nav-up-link]');
    if (!links.length) return;

    const entrySource = getNavEntrySource();
    const target = getUpTarget();
    // No ?from=: fall back to naming whatever getUpTarget() actually resolved, looked up in the same
    // table by filename so the label can't contradict the href.
    const targetFile = target.split('/').pop();
    const matched = Object.values(NAV_ENTRY_SOURCES).find(source => source.file === targetFile);
    const label = (entrySource || matched || { label: 'Dashboard' }).label;

    links.forEach(link => {
        link.setAttribute('href', target);
        link.textContent = '← Back to ' + label;
    });
}

// ---------- BACK: CHRONOLOGICAL HISTORY STACK ----------
// sessionStorage-backed (not a plain JS variable — a real page navigation, e.g. Verification to
// Audit Workbook, reloads this whole module) array of {url, viewKey, label}, capped at 20 entries,
// oldest trimmed first. url is pathname+search; viewKey is null for a plain page load or an opaque
// string a page's own setInPageRestoreHandler() knows how to interpret for an in-page view.
// Follows this project's established try/catch + warn-only + clear-on-parse-error sessionStorage
// convention (see e.g. VERIFICATION_DATE_RANGE_STORAGE_KEY in verification.js).
const NAV_HISTORY_STORAGE_KEY = 'navHistoryStack';
const NAV_PENDING_RESTORE_KEY = 'navPendingRestore';
const NAV_HISTORY_MAX = 20;

function readNavHistory() {
    try {
        const raw = sessionStorage.getItem(NAV_HISTORY_STORAGE_KEY);
        if (!raw) return [];
        const parsed = JSON.parse(raw);
        return Array.isArray(parsed) ? parsed : [];
    } catch (error) {
        console.warn('⚠️ Failed to read nav history, resetting:', error.message);
        sessionStorage.removeItem(NAV_HISTORY_STORAGE_KEY);
        return [];
    }
}

function writeNavHistory(stack) {
    try {
        const trimmed = stack.slice(-NAV_HISTORY_MAX);
        sessionStorage.setItem(NAV_HISTORY_STORAGE_KEY, JSON.stringify(trimmed));
    } catch (error) {
        console.warn('⚠️ Failed to write nav history:', error.message);
    }
}

function currentPageUrl() {
    return window.location.pathname + window.location.search;
}

/**
 * Push the current page's entry onto the Back history stack, skipping a duplicate push if it
 * already matches the current top (e.g. a plain refresh). Called once per page load from
 * initClientContext(), after any pending in-page restore has been consumed.
 */
function pushCurrentPageToHistory() {
    const stack = readNavHistory();
    const entry = { url: currentPageUrl(), viewKey: null, label: window._appNav.module };
    const top = stack[stack.length - 1];
    if (top && top.url === entry.url && top.viewKey === entry.viewKey) return;
    stack.push(entry);
    writeNavHistory(stack);
}

/**
 * Page-facing hook (used by multi-view pages, wired in a later phase): push a new history entry
 * for an in-page view change (no navigation happens — the URL stays the same, only viewKey/label
 * differ), so Back can later restore it via setInPageRestoreHandler().
 * @param {string} viewKey
 * @param {string} label
 */
window.pushInPageNavEntry = function(viewKey, label) {
    const stack = readNavHistory();
    const entry = { url: currentPageUrl(), viewKey: viewKey || null, label: label || window._appNav.module };
    const top = stack[stack.length - 1];
    if (top && top.url === entry.url && top.viewKey === entry.viewKey) return;
    stack.push(entry);
    writeNavHistory(stack);
};

/**
 * On page load, consume a pending in-page restore left behind by a cross-page Back click (see
 * handleBackArrowClick() below). Runs before pushCurrentPageToHistory() so the just-restored entry
 * (already the stack's top from the Back click that navigated here) isn't duplicated.
 */
function consumePendingNavRestore() {
    let pending;
    try {
        const raw = sessionStorage.getItem(NAV_PENDING_RESTORE_KEY);
        if (!raw) return;
        pending = JSON.parse(raw);
    } catch (error) {
        console.warn('⚠️ Failed to read pending nav restore, discarding:', error.message);
        sessionStorage.removeItem(NAV_PENDING_RESTORE_KEY);
        return;
    }
    sessionStorage.removeItem(NAV_PENDING_RESTORE_KEY);
    if (pending && pending.viewKey && typeof window._appNav.inPageRestoreHandler === 'function') {
        window._appNav.inPageRestoreHandler(pending.viewKey);
    }
}

/**
 * Back-arrow click: pop the current entry, then either restore an in-page view on this same page
 * (via inPageRestoreHandler) or navigate to a different page (stashing its viewKey, if any, for
 * that page to consume on load via consumePendingNavRestore()).
 */
function handleBackArrowClick() {
    const stack = readNavHistory();
    if (stack.length <= 1) return false; // nothing to go back to; button is disabled in this state anyway

    stack.pop(); // discard "current" entry
    const newTop = stack[stack.length - 1];
    writeNavHistory(stack);

    if (newTop.url === currentPageUrl()) {
        if (typeof window._appNav.inPageRestoreHandler === 'function') {
            window._appNav.inPageRestoreHandler(newTop.viewKey);
        }
        return false;
    }

    if (newTop.viewKey) {
        try {
            sessionStorage.setItem(NAV_PENDING_RESTORE_KEY, JSON.stringify({ viewKey: newTop.viewKey }));
        } catch (error) {
            console.warn('⚠️ Failed to stash pending nav restore:', error.message);
        }
    }
    window.location.href = newTop.url;
    return true;
}

// ---------- RENDER LEFT SECTION (brand + home icon + back arrow) ----------
function renderNavLeft() {
    const container = document.getElementById('navLeft');
    if (!container) return;

    const isDash = isDashboardPage();
    const homeIcon = `<a href="/static/index.html" class="nav-home-icon" title="Go to Dashboard">🏢</a>`;
    // Logout is no longer rendered here — it moved to the navbar's right side, beside the Welcome
    // text, as an icon-only control (see renderNavUser() below). A session action sat oddly among
    // the brand/nav/search group.
    // Back: chronological history stack (see handleBackArrowClick()) — always intercepted, so a
    // <button> rather than an anchor with a static href. Disabled when there's nowhere to go back
    // to (stack has 0 or 1 entries, i.e. this is the first page visited this session).
    const backDisabled = readNavHistory().length <= 1;
    const backArrow = `<button type="button" class="nav-back-arrow" title="Back — go to the page you just came from"${backDisabled ? ' disabled' : ''}>←</button>`;
    // Up: hierarchical parent (getUpTarget()/upOverride) — also always intercepted, since its
    // destination can depend on a page-registered override rather than a fixed href.
    const upArrow = `<button type="button" class="nav-up-arrow" title="Up — go to the parent screen">↑</button>`;
    // Quick-navigation icon + panel, rendered immediately after the brand text on every page.
    const navMenu = `
        <button type="button" id="navMenuToggle" class="nav-menu-toggle" aria-haspopup="true" aria-expanded="false" title="Quick navigation">⊞</button>
        <div id="navMenuPanel" class="nav-menu-panel" role="menu" hidden></div>
    `;
    // Phase 2: live-search field beside the ⊞ icon, same NAV_MANIFEST data source. Uses the
    // ARIA combobox/listbox pattern — the input keeps real DOM focus the whole time; the
    // "highlighted" result is tracked via aria-activedescendant, not by moving focus onto
    // result rows (see initNavSearch()/renderNavSearchResults() below).
    const navSearch = `
        <div id="navSearchWrapper" class="nav-search-wrapper">
            <input type="text" id="navSearchInput" class="nav-search-input"
                   role="combobox" aria-expanded="false" aria-autocomplete="list"
                   aria-controls="navSearchResults" aria-haspopup="listbox"
                   placeholder="Search... ( / )" title="Search all pages and features" autocomplete="off">
            <div id="navSearchResults" class="nav-menu-panel" role="listbox" hidden></div>
        </div>
    `;

    if (isDash) {
        // Dashboard: home icon + brand, no back arrow (nothing above the dashboard)
        container.innerHTML = `
            ${homeIcon}
            <span style="font-weight:700; font-size:24px; letter-spacing:1px; color:white; margin-left:8px;">System Analyst</span>
            ${navMenu}
            ${navSearch}
        `;
    } else {
        // Non-dashboard: back arrow, up arrow, then home icon, then brand
        container.innerHTML = `
            ${backArrow}
            ${upArrow}
            ${homeIcon}
            <span style="font-weight:700; font-size:24px; letter-spacing:1px; color:white; margin-left:8px;">System Analyst</span>
            ${navMenu}
            ${navSearch}
        `;
    }

    initNavMenu();
    initNavSearch();
}

// ---------- RENDER RIGHT SECTION (welcome text + icon-only logout) ----------
// Fills the .nav-user container every page already has in its navbar markup. That markup used to
// carry a hardcoded "👤 Welcome, ..." span; the span was deleted from all 15 pages and this is now
// the only thing that writes the greeting, so the wording can't drift per page again.
// Selected by class, not id, unlike #navLeft/#clientControl — .nav-user is unique on every page,
// and introducing an id would mean a second unrelated edit to all 15 files for no benefit.
// Logout is icon-only here: title AND aria-label both set, since with the text label gone the emoji
// is the button's only content and an emoji alone is not a reliable accessible name. Its click is
// handled by the existing document-level .nav-logout-btn listener at the bottom of this file, so
// moving it out of renderNavLeft() needed no handler change.
function renderNavUser() {
    const container = document.querySelector('.nav-user');
    if (!container) return;

    container.innerHTML = `
        <span class="nav-user-name">${NAV_USER_LABEL}</span>
        <button type="button" class="nav-logout-btn" title="Log out" aria-label="Log out">🚪</button>
    `;
}

// ---------- RENDER FIRM-LEVEL NAVBAR (settings/users/clients) ----------
// The Admin pages are firm-level, not client-scoped, so the shared header above is actively wrong
// on them: it renders a "Client: None selected" pill, a 🏢 dashboard icon, the ⊞ quick-nav dropdown
// and the search field — every one of which is a route back into a client's workspace these pages
// have nothing to do with. This renders a deliberately minimal replacement instead: firm identity
// and a way out, nothing else.
// Kept here rather than hardcoded into the three HTML pages on purpose. Per-page navbar markup has
// already drifted apart once in this app (the "Welcome, Admin" vs "Welcome, Analyst" split across
// 15 pages that NAV_USER_LABEL was introduced to end) — one renderer can't drift.
// Deliberately ABSENT, do not "restore" any of these: the client pill, the 🏢 home icon, the Back
// and Up arrows, the ⊞ dropdown (initNavMenu) and the search field (initNavSearch).
// The brand text links to client_select.html — firm-level, and the Admin area's own sanctioned
// entry point (its gear icon) — so it is a way back that never enters client territory. Logout
// reuses the existing .nav-logout-btn class, so the document-level delegated listener at the bottom
// of this file already handles its click; no listener is bound here.
function renderFirmLevelNav() {
    const container = document.getElementById('firmNav');
    if (!container) return;

    container.innerHTML = `
        <div class="firm-nav-left">
            <a href="/static/client_select.html" class="firm-nav-brand" title="Back to client selection">System Analyst</a>
            <span class="firm-nav-area">AREA: Administration</span>
        </div>
        <div class="firm-nav-right">
            <span class="nav-user-name">${NAV_USER_LABEL}</span>
            <button type="button" class="nav-logout-btn" title="Log out" aria-label="Log out">🚪</button>
        </div>
    `;
}

// ---------- FIRM-LEVEL TAB STRIP (settings.html / users.html / clients.html) ----------
// The Admin area's persistent tab navigation, defined once here for the same reason
// renderFirmLevelNav() above is: it used to be hardcoded inline in settings.html only, so clicking
// "Manage Clients"/"Manage Users" landed the user on a page with no tab strip at all and no way
// back to the other pages. One renderer can't drift the way per-page markup did.
// A hand-maintained data array, deliberately not a scrape of any page's DOM - same convention as
// NAV_MANIFEST above. `file` doubles as both the navigation target and the active-state key.
const FIRM_LEVEL_TABS = [
    { file: 'settings.html', label: '⚙️ App Settings', adminOnly: false },
    { file: 'clients.html', label: '🏢 Manage Clients', adminOnly: true },
    { file: 'users.html', label: '👥 Manage Users', adminOnly: true },
    { file: 'storage_cleanup.html', label: '🧹 Storage Cleanup', adminOnly: true }
];

// Memoised admin probe. There is still no dedicated "am I admin" endpoint, so this reuses the same
// de facto GET /api/users 403-probe that settings.js/clients.js/client_select.js already rely on -
// memoising the promise means the Admin pages issue one probe rather than each firing its own.
// Fails closed (false) on any error, matching every existing call site's behaviour.
let adminProbePromise = null;

function isAdminUser() {
    if (isFirmLevelPage() && window.AppTenantAdministration)
        return Promise.resolve(window.AppTenantAdministration.canAdminister === true);
    if (!adminProbePromise) {
        adminProbePromise = fetch(`${API_BASE}/api/users`)
            .then(res => res.ok)
            .catch(() => false);
    }
    return adminProbePromise;
}

// Memoised clients list cache (Fast Data Handling, Phase 8) - clients.js's loadClients()
// re-fetches this same include_inactive=true list on every page action; caching it here
// (not in clients.js) keeps the fetch/cache logic next to the other shared reference-data
// fetches this file already owns. forceRefresh=true bypasses the cache after a mutation
// clients.js knows changed server state (create/edit/delete/restore).
let clientsListCache = null;

async function getClientsList(forceRefresh = false) {
    if (clientsListCache && !forceRefresh) return clientsListCache;
    const res = await fetch(`${API_BASE}/api/clients?include_inactive=true`);
    if (!res.ok) throw new Error('Failed to fetch clients');
    clientsListCache = await res.json();
    return clientsListCache;
}

// Fills the #firmTabs container each Admin page declares (an empty div - the page supplies no tab
// markup of its own). Active state is derived from the current filename, the same read
// isFirmLevelPage() uses, so "you are here" always comes from the URL rather than a hardcoded
// class in HTML - which is what made the old active state dead once you navigated away.
// Admin-only tabs render hidden and are revealed once isAdminUser() resolves true, preserving
// settings.html's existing behaviour where a non-admin sees App Settings alone.
function renderFirmLevelTabs() {
    const container = document.getElementById('firmTabs');
    if (!container) return;

    const currentFile = window.location.pathname.split('/').pop() || '';

    container.innerHTML = FIRM_LEVEL_TABS.map(tab => {
        const isActive = tab.file === currentFile;
        return `
            <button type="button"
                    class="firm-tab${isActive ? ' active' : ''}"
                    data-tab-file="${tab.file}"
                    ${tab.adminOnly ? 'data-admin-only="true" style="display:none;"' : ''}
                    ${isActive ? 'aria-current="page"' : ''}>${tab.label}</button>
        `;
    }).join('');

    isAdminUser().then(isAdmin => {
        if (!isAdmin) return;
        container.querySelectorAll('[data-admin-only]').forEach(tab => {
            tab.style.display = '';
        });
    });
}

// Bound once at module load rather than inside renderFirmLevelTabs(), so a future re-render can
// never stack duplicate listeners - the same reasoning as the nav-menu document listeners below.
// Looks the container up fresh each time since its contents are replaced on render.
document.addEventListener('click', function (e) {
    const tab = e.target.closest('.firm-tab');
    if (!tab) return;
    // Clicking the tab you're already on is a deliberate no-op - no reload, no flicker.
    if (tab.classList.contains('active')) return;
    if (!startNavigationLoading(tab)) return;
    window.location.href = '/static/' + tab.dataset.tabFile;
});

// ---------- QUICK NAVIGATION MENU (⊞ icon + dropdown) ----------
// Re-initialised on every renderNavLeft() call since the toggle/panel are rebuilt via
// innerHTML each time (mirrors how renderClientControl() re-binds clientSelectElement
// after its own innerHTML rebuild, above). Only the toggle/panel's own listeners are
// (re)bound here, since those elements themselves are freshly created each call; the
// document-level outside-click/Escape listeners are bound exactly once at module load
// (below) and always look up the current toggle/panel fresh, so they never duplicate
// across repeated renders (module switches, client changes, etc.).
function initNavMenu() {
    const toggle = document.getElementById('navMenuToggle');
    const panel = document.getElementById('navMenuPanel');
    if (!toggle || !panel) return;

    panel.innerHTML = renderNavMenuPanel();

    toggle.addEventListener('click', function (e) {
        e.stopPropagation();
        if (panel.hidden) {
            openNavMenu(toggle, panel);
        } else {
            closeNavMenu(toggle, panel, false);
        }
    });
}

// Bound once at module load (not inside initNavMenu()) so repeated renderNavLeft() calls
// never stack duplicate document-level listeners. Both look up the live toggle/panel by
// id each time, since those elements are replaced on every render.
document.addEventListener('click', function (e) {
    const toggle = document.getElementById('navMenuToggle');
    const panel = document.getElementById('navMenuPanel');
    if (!toggle || !panel || panel.hidden) return;
    if (!panel.contains(e.target) && e.target !== toggle) {
        closeNavMenu(toggle, panel, false);
    }
});

document.addEventListener('keydown', function (e) {
    const toggle = document.getElementById('navMenuToggle');
    const panel = document.getElementById('navMenuPanel');
    if (!toggle || !panel || panel.hidden) return;

    const items = getNavMenuFocusableItems(panel);
    const currentIndex = items.indexOf(document.activeElement);

    if (e.key === 'Escape') {
        e.preventDefault();
        closeNavMenu(toggle, panel, true);
    } else if (e.key === 'ArrowDown' && panel.contains(document.activeElement)) {
        e.preventDefault();
        const next = items[(currentIndex + 1) % items.length];
        if (next) next.focus();
    } else if (e.key === 'ArrowUp' && panel.contains(document.activeElement)) {
        e.preventDefault();
        const prev = items[(currentIndex - 1 + items.length) % items.length];
        if (prev) prev.focus();
    }
});

/**
 * Build the dropdown's inner HTML from NAV_MANIFEST: one labelled group per section,
 * one row per item (nested one indent level for Verification's Cashbook/Bank book
 * children). Ready items render as real links; coming-soon items (and all sub-view
 * children this phase) render as inert, non-focusable-via-click spans.
 */
function renderNavMenuPanel() {
    const currentModule = window._appNav?.module || null;

    function renderItem(item, isChild) {
        const isCurrent = item.state === 'ready' && item.module && item.module === currentModule;
        const classes = [
            'nav-menu-item',
            item.state === 'coming-soon' ? 'coming-soon' : '',
            isCurrent ? 'current' : '',
            isChild ? 'nav-menu-child' : '',
        ].filter(Boolean).join(' ');

        let html;
        if (item.state === 'ready') {
            html = `<a href="${resolveNavItemUrl(item)}" role="menuitem" class="${classes}">${item.label}</a>`;
        } else {
            html = `<span role="menuitem" aria-disabled="true" class="${classes}">${item.label}</span>`;
        }

        if (item.children && item.children.length) {
            html += item.children.map(child => renderItem(child, true)).join('');
        }
        return html;
    }

    return NAV_MANIFEST.map(group => `
        <div class="nav-menu-group">
            <div class="nav-menu-group-title">${group.title}</div>
            ${group.items.map(item => renderItem(item, false)).join('')}
        </div>
    `).join('');
}

function getNavMenuFocusableItems(panel) {
    return Array.from(panel.querySelectorAll('[role="menuitem"]:not([aria-disabled])'));
}

// ---------- VIEWPORT-AWARE PANEL POSITIONING (shared by the Phase 1 dropdown and Phase 2 search
// results — both use the .nav-menu-panel class/CSS, but anchor to different positioned ancestors:
// #navLeft for the dropdown, #navSearchWrapper for search. Both call sites (openNavMenu() below,
// renderNavSearchResults() further down) pass their own anchor into this one function so the fix
// lives in exactly one place. ----------
// Bug fixed (horizontal): the panel's CSS position (left:0, no width bound) let it render
// partially/fully off-screen whenever its anchor sat close enough to the right edge that a
// min-width:240px panel wouldn't fit. Fixed by measuring the anchor's real position via
// getBoundingClientRect() against window.innerWidth at the moment the panel opens, then choosing
// left- vs right-alignment and clamping width.
// Bug fixed (vertical, follow-up): the panel always opened downward (top:100%, fixed in CSS) with
// no max-height/overflow-y at all, so a tall item list (e.g. verification.html's full manifest)
// could extend below the visible browser window/taskbar. Fixed the same way: measure against
// window.innerHeight, flip to open upward when that has more room, and — per the user's confirmed
// choice — clamp height and let the panel scroll internally if neither direction has enough room,
// matching the existing max-height+overflow-y convention already used by .transaction-list-popover
// and .voucher-change-popover/.audit-guidance-popover elsewhere in this app (verification.js/
// style.css), rather than inventing a new pattern.
// Neither axis re-runs on window resize while a panel is already open (not part of the confirmed
// scope for either fix) — closing and reopening re-measures correctly.
function positionNavPanel(anchorEl, panel) {
    if (!anchorEl || !panel) return;

    // Clear any inline overrides from a previous open (e.g. under a different viewport size)
    // before measuring, so stale values from an earlier open can't leak into this one.
    panel.style.left = '';
    panel.style.right = '';
    panel.style.maxWidth = '';
    panel.style.minWidth = '';
    panel.style.top = '';
    panel.style.bottom = '';
    panel.style.marginTop = '';
    panel.style.marginBottom = '';
    panel.style.maxHeight = '';
    panel.style.overflowY = '';

    const anchorRect = anchorEl.getBoundingClientRect();
    const viewportWidth = window.innerWidth;
    const viewportHeight = window.innerHeight;
    const edgeMargin = 12; // small breathing room from the viewport edge, not flush against it
    // Real rendered size now that panel.hidden is false and inline overrides were just cleared
    // (must be called after unhiding — an element with display:none, which [hidden] sets, always
    // measures 0 — and before re-applying max-height, or this would measure the clamped size
    // instead of the panel's true natural size).
    const naturalWidth = panel.offsetWidth;
    const naturalHeight = panel.offsetHeight;

    // ---- Horizontal (left/right) ----
    const spaceRight = viewportWidth - anchorRect.left - edgeMargin;
    const spaceLeft = anchorRect.right - edgeMargin;

    let availableWidth;
    if (naturalWidth <= spaceRight) {
        // Default left-aligned anchor (CSS left:0) already fits — keep it, just cap max-width
        // to the real available space as a safeguard against a future wider panel.
        availableWidth = spaceRight;
    } else if (naturalWidth <= spaceLeft) {
        // Doesn't fit opening rightward from the anchor's left edge, but does fit opening
        // leftward from the anchor's right edge — flip to right-aligned.
        panel.style.left = 'auto';
        panel.style.right = '0';
        availableWidth = spaceLeft;
    } else {
        // Neither side has room for the panel's natural width (e.g. a very narrow viewport) —
        // pick whichever side has more space and clamp hard so it still can't overflow.
        if (spaceRight >= spaceLeft) {
            availableWidth = spaceRight;
        } else {
            panel.style.left = 'auto';
            panel.style.right = '0';
            availableWidth = spaceLeft;
        }
    }

    // Floor at a small usable width so an extreme viewport never collapses the panel to nothing.
    const clampedWidth = Math.max(160, availableWidth);
    panel.style.maxWidth = clampedWidth + 'px';
    // The CSS min-width:240px would otherwise win over max-width when they conflict (min-width
    // takes priority), forcing overflow right back in on a tight viewport — override it down to
    // match whenever the clamp is tighter than that default.
    panel.style.minWidth = Math.min(240, clampedWidth) + 'px';

    // ---- Vertical (top/bottom) ----
    // margin-top:8px on the default (downward) direction comes from CSS; mirrored manually as
    // margin-bottom when flipped upward, so the same 8px gap from the anchor applies either way.
    const spaceBelow = viewportHeight - anchorRect.bottom - edgeMargin - 8;
    const spaceAbove = anchorRect.top - edgeMargin - 8;

    let availableHeight;
    if (naturalHeight <= spaceBelow) {
        // Default downward open (CSS top:100%) already fits — keep it, just cap max-height as a
        // safeguard against a future taller panel.
        availableHeight = spaceBelow;
    } else if (naturalHeight <= spaceAbove) {
        // Doesn't fit opening downward, but does fit opening upward — flip above the anchor.
        panel.style.top = 'auto';
        panel.style.bottom = '100%';
        panel.style.marginTop = '0';
        panel.style.marginBottom = '8px';
        availableHeight = spaceAbove;
    } else {
        // Neither direction has room for the panel's natural height (a tall item list on a short
        // window) — open on whichever side has more space and let the panel scroll internally for
        // the rest, per the confirmed choice and the existing popover convention in this app.
        if (spaceBelow >= spaceAbove) {
            availableHeight = spaceBelow;
        } else {
            panel.style.top = 'auto';
            panel.style.bottom = '100%';
            panel.style.marginTop = '0';
            panel.style.marginBottom = '8px';
            availableHeight = spaceAbove;
        }
    }

    // Floor at a small usable height so an extreme viewport never collapses the panel to nothing;
    // overflow-y is always set (not just in the "still doesn't fit" branch) so it's a harmless
    // no-op when there's room and a real scroll fallback the moment there isn't.
    const clampedHeight = Math.max(120, availableHeight);
    panel.style.maxHeight = clampedHeight + 'px';
    panel.style.overflowY = 'auto';
}

function openNavMenu(toggle, panel) {
    panel.hidden = false;
    toggle.setAttribute('aria-expanded', 'true');
    positionNavPanel(document.getElementById('navLeft'), panel);
    const items = getNavMenuFocusableItems(panel);
    if (items.length) items[0].focus();
}

function closeNavMenu(toggle, panel, returnFocus) {
    panel.hidden = true;
    toggle.setAttribute('aria-expanded', 'false');
    if (returnFocus) toggle.focus();
}

// ---------- QUICK NAVIGATION SEARCH (Phase 2) ----------
// Index of the currently keyboard-highlighted result within the ready-only result set
// (see getSearchFocusableResults()); -1 means "nothing explicitly highlighted yet", in
// which case Enter falls back to the first ready result, per the confirmed spec.
let searchHighlightIndex = -1;

/**
 * Re-bound on every renderNavLeft() call since the input/panel are rebuilt via innerHTML
 * each time (same reasoning as initNavMenu()). Only the input's own listeners are (re)bound
 * here; the outside-click listener is bound once at module load, below.
 */
function initNavSearch() {
    const input = document.getElementById('navSearchInput');
    const panel = document.getElementById('navSearchResults');
    if (!input || !panel) return;

    input.addEventListener('input', function () {
        renderNavSearchResults(input.value);
    });

    input.addEventListener('keydown', function (e) {
        if (e.key === 'Escape') {
            e.preventDefault();
            input.value = '';
            closeNavSearch(input, panel);
        } else if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
            if (panel.hidden) return;
            e.preventDefault();
            const results = getSearchFocusableResults(panel);
            if (!results.length) return;
            if (e.key === 'ArrowDown') {
                searchHighlightIndex = (searchHighlightIndex + 1) % results.length;
            } else {
                searchHighlightIndex = (searchHighlightIndex - 1 + results.length) % results.length;
            }
            applySearchHighlight(input, results);
        } else if (e.key === 'Enter') {
            if (panel.hidden) return;
            e.preventDefault();
            const results = getSearchFocusableResults(panel);
            const target = results[searchHighlightIndex] || results[0];
            if (target) {
                target.click();
            }
        }
    });
}

// Bound once at module load (mirrors the nav-menu outside-click listener's reasoning above)
// so repeated renderNavLeft() calls never stack duplicate document-level listeners. Closes
// the results panel without clearing the typed text — deliberately different from Escape.
document.addEventListener('click', function (e) {
    const wrapper = document.getElementById('navSearchWrapper');
    const panel = document.getElementById('navSearchResults');
    if (!wrapper || !panel || panel.hidden) return;
    if (!wrapper.contains(e.target)) {
        panel.hidden = true;
        const input = document.getElementById('navSearchInput');
        if (input) input.setAttribute('aria-expanded', 'false');
    }
});

/**
 * Filter NAV_MANIFEST (via flattenNavManifest()) by a case-insensitive substring match
 * against each entry's label, and render the matches grouped exactly like Phase 1's
 * dropdown (group order/titles come straight from NAV_MANIFEST, never duplicated).
 * Reuses the .nav-menu-panel/.nav-menu-group/.nav-menu-item classes Phase 1 already
 * built, so the two mechanisms share one visual language.
 */
function renderNavSearchResults(query) {
    const input = document.getElementById('navSearchInput');
    const panel = document.getElementById('navSearchResults');
    if (!input || !panel) return;

    const trimmed = query.trim();
    searchHighlightIndex = -1;

    if (!trimmed) {
        panel.hidden = true;
        panel.innerHTML = '';
        input.setAttribute('aria-expanded', 'false');
        input.removeAttribute('aria-activedescendant');
        return;
    }

    const needle = trimmed.toLowerCase();
    const matches = flattenNavManifest().filter(entry =>
        entry.label.toLowerCase().includes(needle) ||
        (entry.keywords || []).some(kw => kw.includes(needle))
    );

    if (!matches.length) {
        panel.innerHTML = '<div class="nav-search-empty">No matches found.</div>';
    } else {
        let optionIndex = 0;
        const groupsHtml = NAV_MANIFEST.map(group => {
            const groupMatches = matches.filter(m => m.groupTitle === group.title);
            if (!groupMatches.length) return '';
            const rowsHtml = groupMatches.map(entry => {
                const classes = [
                    'nav-menu-item',
                    entry.state === 'coming-soon' ? 'coming-soon' : '',
                    entry.isChild ? 'nav-menu-child' : '',
                ].filter(Boolean).join(' ');
                if (entry.state === 'ready') {
                    const id = 'navSearchOpt-' + optionIndex;
                    optionIndex += 1;
                    return `<a href="${resolveNavItemUrl(entry)}" role="option" id="${id}" class="${classes}">${entry.label}</a>`;
                }
                return `<span role="option" aria-disabled="true" class="${classes}">${entry.label}</span>`;
            }).join('');
            return `<div class="nav-menu-group"><div class="nav-menu-group-title">${group.title}</div>${rowsHtml}</div>`;
        }).join('');
        panel.innerHTML = groupsHtml;
    }

    panel.hidden = false;
    // Shared with the Phase 1 dropdown (see positionNavPanel() above) — anchored to
    // #navSearchWrapper (the search input's own box) rather than #navLeft, since that's this
    // panel's actual positioned ancestor.
    positionNavPanel(document.getElementById('navSearchWrapper'), panel);
    input.setAttribute('aria-expanded', 'true');
}

// Only ready (actionable) rows participate in keyboard highlighting/Enter — coming-soon
// rows are visible but never targetable, matching Phase 1's dropdown convention exactly.
function getSearchFocusableResults(panel) {
    return Array.from(panel.querySelectorAll('a[role="option"]'));
}

function applySearchHighlight(input, results) {
    results.forEach((el, i) => {
        el.classList.toggle('highlighted', i === searchHighlightIndex);
    });
    const active = results[searchHighlightIndex];
    if (active) {
        input.setAttribute('aria-activedescendant', active.id);
    } else {
        input.removeAttribute('aria-activedescendant');
    }
}

function closeNavSearch(input, panel) {
    panel.hidden = true;
    panel.innerHTML = '';
    input.setAttribute('aria-expanded', 'false');
    input.removeAttribute('aria-activedescendant');
    searchHighlightIndex = -1;
    input.focus();
}

// Ready links are rendered by both the quick-nav menu and its search results. Let the browser keep
// its normal navigation semantics while marking the transition; modified/new-tab clicks stay free
// of a cursor token because this document remains open after those clicks.
document.addEventListener('click', function (e) {
    if (e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
    const link = e.target.closest('a.nav-menu-item, a.firm-nav-brand');
    if (!link || link.target === '_blank') return;
    if (!startNavigationLoading(link)) e.preventDefault();
});

// ---------- GLOBAL KEYBOARD SHORTCUT: "/" FOCUSES SEARCH ----------
// Bound once at module load (same reasoning as the other document-level listeners in this file —
// #navSearchInput is rebuilt via innerHTML on every renderNavLeft() call, so this looks it up fresh
// by id each time rather than caching a reference). Guards on the currently focused element so
// typing a literal "/" into any other input/textarea/contenteditable field anywhere in the app is
// never intercepted — the app's existing global keydown handlers (e.g. script.js's "R"-to-refresh)
// have no such guard, which is a gap this shortcut deliberately avoids repeating.
document.addEventListener('keydown', function (e) {
    if (e.key !== '/') return;
    const active = document.activeElement;
    const isEditable = active && (
        active.tagName === 'INPUT' || active.tagName === 'TEXTAREA' || active.isContentEditable
    );
    if (isEditable) return;
    const input = document.getElementById('navSearchInput');
    if (!input) return;
    e.preventDefault();
    input.focus();
    input.select();
});

// ---------- GLOBAL CLICK HANDLER: BACK ARROW (chronological history stack) ----------
document.addEventListener('click', function (e) {
    const backArrow = e.target.closest('.nav-back-arrow');
    if (backArrow && !backArrow.disabled) {
        if (!startNavigationLoading(backArrow)) return;
        if (!handleBackArrowClick()) stopNavigationLoading();
    }
});

// ---------- GLOBAL CLICK HANDLER: UP ARROW (hierarchical parent) ----------
// When a page has registered a dynamic override via setUpOverride() (e.g. Verification's in-page
// view switching), run that instead of navigating to the static getUpTarget() URL.
document.addEventListener('click', function (e) {
    const homeIcon = e.target.closest('.nav-home-icon');
    if (homeIcon && typeof window.leaveVerificationSafely === 'function') {
        e.preventDefault();
        if (navigationLoadingToken) return;
        window.leaveVerificationSafely(function() {
            if (!startNavigationLoading(homeIcon)) return;
            window.location.href = homeIcon.href;
        });
        return;
    }
});

document.addEventListener('click', function (e) {
    const upArrow = e.target.closest('.nav-up-arrow');
    if (!upArrow) return;
    if (!startNavigationLoading(upArrow)) return;
    if (typeof window._appNav.upOverride === 'function') {
        try {
            const currentPath = window.location.pathname;
            const result = window._appNav.upOverride();
            if (result && typeof result.then === 'function') {
                result.then(stopNavigationLoading, stopNavigationLoading);
            } else if (window.location.pathname === currentPath) {
                // Synchronous in-page view changes are complete when the override returns. A
                // cross-page override keeps the token until pagehide replaces this document.
                stopNavigationLoading();
            }
        } catch (error) {
            stopNavigationLoading();
            console.warn('⚠️ Up navigation failed:', error.message);
        }
    } else {
        window.location.href = getUpTarget();
    }
});

// ---------- GLOBAL CLICK HANDLER: LOGOUT ----------
document.addEventListener('click', function (e) {
    const logoutBtn = e.target.closest('.nav-logout-btn');
    if (logoutBtn) {
        logoutUser(logoutBtn);
    }
});

// ---------- GLOBAL CLICK HANDLER: CLIENT DISPLAY (switch client) ----------
// Delegated by necessity, not style: renderClientControl() rebuilds this button via innerHTML on
// every setNavModule()/setNavStats() call, so a listener bound directly to the element would be
// thrown away on the first re-render (e.g. verification.js setting its module name at runtime).
// The two states are told apart by the button's own client-none class rather than by re-reading
// session state, so what's rendered is the single source of truth for what the click does.
document.addEventListener('click', function (e) {
    const display = e.target.closest('.nav-client-display');
    if (!display) return;
    if (navigationLoadingToken) return;
    if (display.classList.contains('client-none')) {
        // Nothing selected yet — nothing to lose, so skip the confirm entirely.
        goToClientSelect(display);
    } else {
        confirmSwitchClient(display);
    }
});
// ---------- RENDER CLIENT BADGE (read-only) ----------
function renderClientBadge() {
    const container = document.getElementById('clientControl');
    if (!container) return;

    const clientId = getSelectedClientId();
    const clientName = getSelectedClientName();
    const clientCode = getSelectedClientCode();

    if (clientId && clientName) {
        container.innerHTML = `
            <span class="client-badge">
                🏢 ${clientName} (${clientCode})
                <button class="client-switch-btn" title="Switch Client" onclick="confirmSwitchClient()">🔄</button>
            </span>
        `;
    } else {
        container.innerHTML = `
            <span class="client-badge client-none">
                ⚠️ No client selected
                <button class="client-switch-btn" title="Go to Dashboard to select client" onclick="goToDashboard()">🏠</button>
            </span>
        `;
    }
}

// ---------- SWITCH CLIENT FLOW ----------
// One confirm, then straight to the client picker. Two things changed here from the original:
// it used to navigate to index.html, which stopped being a picker once the dashboard dropdown was
// removed; and it fired a second alert() warning that "all client-related data visible on current
// pages will be cleared", which was never accurate under the session-backed design — every query
// is client_id-scoped, so nothing mixes and nothing is discarded by switching. Cancel does nothing.
function confirmSwitchClient(target) {
    if (confirm('Switch client? Any unsaved work on this page may be lost.')) {
        goToClientSelect(target);
    }
}

function goToClientSelect(target) {
    if (!startNavigationLoading(target)) return;
    window.location.href = '/static/client_select.html';
}

function goToDashboard(target) {
    if (!startNavigationLoading(target)) return;
    window.location.href = '/static/index.html';
}

// ---------- INIT ----------
async function initClientContext() {
    // Data-attribute source for the Area label (mirrors getUpTarget()'s data-nav-parent read) -
    // lets a self-contained page declare its module name via <body data-nav-module="..."> instead
    // of needing its own setNavModule() call. Read before the first render so it's correct on
    // first paint; setNavModule() can still override it later (e.g. verification.js at runtime).
    const moduleAttr = document.body.getAttribute('data-nav-module');
    if (moduleAttr) {
        window._appNav.module = moduleAttr;
    }
    // Consume any pending in-page restore left by a cross-page Back click before pushing this
    // page's own history entry, so the restored entry isn't duplicated (see
    // consumePendingNavRestore()/pushCurrentPageToHistory() above).
    consumePendingNavRestore();
    // Firm-level Admin pages stay out of the Back history stack entirely. They are reached only via
    // client_select.html's gear icon, never as part of a client's workflow, so letting them onto the
    // stack would make an Admin page the Back destination of a client-scoped page later in the
    // session - exactly the cross-territory jump this whole change removes.
    if (!isFirmLevelPage() && !isClientSettingsAdminDoor()) {
        pushCurrentPageToHistory();
    }
    // Populate the session-backed client cache before anything renders that depends on it
    // (the client badge/dropdown). This is a real network fetch, and window.onload waiting on
    // subresources says nothing about an in-flight fetch() — on a light page onload fires first.
    // Page scripts must therefore await window.clientContextReady (resolved below) rather than
    // assuming their own window.onload runs after this.
    const sessionValid = await loadSessionClient();
    if (!sessionValid) {
        // loadSessionClient() already redirected to login.html on a 401. Stop here rather
        // than rendering the navbar against a null client cache - that render is what used
        // to fire enforceClientSelection()'s alert and a second, competing redirect to
        // index.html. clientContextReady is deliberately left unresolved: the page is
        // navigating away, so any page script awaiting it should never wake back up here.
        return;
    }
    // A no-op on every page except one carrying a whitelisted, valid ?client_id= (see
    // hasUrlSuppliedClient() above) - resolves that client's NAME so the navbar pill can show it
    // instead of "None selected" on an admin-door visit with no session client selected.
    await loadUrlClient();
    // Firm-level Admin pages get their own independent header instead of the client-scoped one -
    // see renderFirmLevelNav() above for what it deliberately leaves out and why. Everything before
    // this point (the pending-restore consume, loadSessionClient() and its 401 early-return) is
    // shared by both branches, so the session guard behaves identically either way.
    if (isFirmLevelPage()) {
        renderFirmLevelNav();
        // The Admin area's persistent tab strip. Rendered into each Admin page's own #firmTabs
        // container (page content, under the module header) rather than into the navbar - see
        // renderFirmLevelTabs() above.
        renderFirmLevelTabs();
    } else if (isClientSettingsAdminDoor()) {
        // client_settings.html via the Admin door: same firm-level navbar as the three Admin
        // pages, but no tab strip (this isn't one of them) and no FIRM_LEVEL_PAGES membership
        // (it must not get this chrome on the other door). The page's own in-page "← Back to ..."
        // link resolves via ?from= the same as any client-scoped page, so renderUpLinks() still runs.
        document.getElementById('clientSettingsNavbar').classList.add('firm-navbar');
        document.getElementById('clientScopedNavContainer').style.display = 'none';
        document.getElementById('firmNav').style.display = '';
        renderFirmLevelNav();
        renderUpLinks();
    } else {
        renderClientControl();
        renderNavLeft();
        // Entry-aware in-page back-links (opt-in via data-nav-up-link). Rendered alongside the
        // navbar so the link and the Up arrow are resolved from the same getUpTarget() call.
        renderUpLinks();
        // Renders no session-dependent data, so its position relative to loadSessionClient() doesn't
        // matter — kept with the other two so clientContextReady keeps meaning "the whole navbar is
        // ready". Not called from setNavModule()/setNavStats(), which re-render only the client
        // control; this content is static for the life of the page.
        renderNavUser();
    }
    // Resolved only once the client cache is populated AND the navbar is rendered, so a page
    // awaiting this gets both.
    resolveClientContextReady();
}

// ---------- EXPOSE GLOBALLY ----------
window.getSelectedClientId = getSelectedClientId;
window.getSelectedClientName = getSelectedClientName;
window.getSelectedClientCode = getSelectedClientCode;
window.refreshSelectedClientContext = async function () {
    const sessionValid = await loadSessionClient();
    return sessionValid ? getSelectedClientId() : null;
};
window.isClientSelected = isClientSelected;
window.confirmSwitchClient = confirmSwitchClient;
window.goToDashboard = goToDashboard;
window.renderClientBadge = renderClientBadge;
window.enforceClientSelection = enforceClientSelection;
// Shared admin probe - exposed so an Admin page's own guard (clients.js's checkAdminAccess())
// reuses this one memoised request instead of issuing a second identical one.
window.isAdminUser = isAdminUser;
window.getClientsList = getClientsList;
window.onClientChange = null; // Hook for page-specific logic

// ---------- AUTO-INIT ON DOM READY ----------
if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', initClientContext);
} else {
    initClientContext();
}
