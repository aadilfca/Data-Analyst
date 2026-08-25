/* Shared apiFetch() utility — debounce + GET-only single retry + a "Retrying..." toast.
   Namespaced under window.ApiUtils rather than bare globals, same collision-risk reasoning
   as loading_ui.js's window.LoadingUI. Self-installing classic <script>: injects its own
   toast markup into <body> on load, same pattern as procedure_modal.js's own markup
   injection. Not yet wired into any page <script> tag — that's later phases (6, 7, 12, 13). */
(function () {
    'use strict';

    // Captured before window.fetch is overridden below (the bypass guard) — every internal
    // call in this file must go through this, never window.fetch, or it would recurse into
    // its own guard.
    const nativeFetch = window.fetch.bind(window);
    const INTERNAL_TAG = '__apiUtilsInternal';

    // Shallow-copies options rather than mutating the caller's object, since a caller may
    // reuse/inspect its options object after the call.
    function taggedOptions(options) {
        return Object.assign({}, options, { [INTERNAL_TAG]: true });
    }

    // ---- Toast markup (self-injected, mirrors procedure_modal.js's #voucherVerifyToast) ----
    const TOAST_MARKUP = '<div id="apiRetryToast" class="api-retry-toast">Retrying...</div>';
    document.body.insertAdjacentHTML('beforeend', TOAST_MARKUP);
    const retryToast = document.getElementById('apiRetryToast');

    function showRetryToast() {
        if (!retryToast) return;
        retryToast.classList.add('show');
    }

    function hideRetryToast() {
        if (!retryToast) return;
        retryToast.classList.remove('show');
    }

    const RETRY_DELAY_MS = 1500;
    const DEFAULT_DEBOUNCE_MS = 400;

    function delay(ms) {
        return new Promise(function (resolve) { setTimeout(resolve, ms); });
    }

    // Retry-eligible: a thrown network error, or a 5xx (transient server-side failure).
    // A 4xx is a real error and is never retried - retrying won't fix a client error.
    function isRetryEligible(error, response) {
        if (error) return true;
        return !!response && response.status >= 500;
    }

    // Single retry, GET only. Non-GET methods and non-retry-eligible failures pass through
    // on the first attempt (thrown error rethrown, response returned as-is).
    async function fetchWithRetry(url, options) {
        const method = (options.method || 'GET').toUpperCase();
        let firstError = null;
        let firstResponse = null;
        try {
            firstResponse = await nativeFetch(url, taggedOptions(options));
        } catch (err) {
            firstError = err;
        }

        if (method !== 'GET' || !isRetryEligible(firstError, firstResponse)) {
            if (firstError) throw firstError;
            return firstResponse;
        }

        showRetryToast();
        try {
            await delay(RETRY_DELAY_MS);
            return await nativeFetch(url, taggedOptions(options));
        } finally {
            hideRetryToast();
        }
    }

    // Per-key pending debounce state, so a superseded call's promise can be rejected
    // rather than left dangling.
    const debounceState = new Map();

    function DebouncedError() {
        const err = new Error('apiFetch call superseded by a newer debounced call');
        err.debounced = true;
        return err;
    }

    function debouncedFetch(url, options, config) {
        const key = config.debounceKey || url;
        const delayMs = typeof config.debounce === 'number' ? config.debounce : DEFAULT_DEBOUNCE_MS;

        const pending = debounceState.get(key);
        if (pending) {
            clearTimeout(pending.timer);
            pending.reject(DebouncedError());
        }

        return new Promise(function (resolve, reject) {
            const timer = setTimeout(function () {
                debounceState.delete(key);
                fetchWithRetry(url, options).then(resolve, reject);
            }, delayMs);
            debounceState.set(key, { timer: timer, reject: reject });
        });
    }

    // apiFetch(url, options, config) - same call contract as native fetch() (resolves to a
    // Response), so a call site can switch from fetch(url, opts) to ApiUtils.apiFetch(url, opts)
    // with no other change.
    //   config.debounce: true (-> 400ms) or a custom ms number. Omitted = no debounce.
    //   config.debounceKey: groups debounced calls; defaults to url.
    function apiFetch(url, options, config) {
        const opts = options || {};
        const cfg = config || {};
        if (cfg.debounce) {
            return debouncedFetch(url, opts, cfg);
        }
        return fetchWithRetry(url, opts);
    }

    // apiFetchMutate(url, options, config) - the apiFetch sibling for POST/PUT/DELETE calls.
    // Same fetch()-contract (resolves to a Response) but never retries (retry-on-write risks
    // double-submits) and never debounces. config.target, if given an element, gets a
    // LoadingUI spinner for the call's duration; config.message is its label (default 'Saving').
    // Both are optional - omitted (or LoadingUI not loaded on the page) means a plain tagged
    // fetch with no spinner, never a throw for that reason.
    function apiFetchMutate(url, options, config) {
        const opts = options || {};
        const cfg = config || {};
        const target = cfg.target;
        if (target && window.LoadingUI) {
            window.LoadingUI.showSpinner(target, cfg.message || 'Saving');
        }
        return nativeFetch(url, taggedOptions(opts)).finally(function () {
            if (target && window.LoadingUI) window.LoadingUI.hideSpinner(target);
        });
    }

    // ---- Dev-console bypass guard ----
    // Warns (once per endpoint+method per page session) when something calls fetch() against
    // /api/ directly instead of through apiFetch/apiFetchMutate above, so a future file can't
    // silently skip this wiring the way procedure_modal.js originally did. Purely advisory -
    // never throws, never alters the request/response, and always passes through to the real
    // fetch unchanged.
    const warnedKeys = new Set();

    function dedupKey(method, url) {
        // Querystring stripped - the goal is "flag this call site in the source once", not
        // once per distinct client_id/upload_id/etc. value passed on a given call.
        const path = String(url).split('?')[0];
        return method + ' ' + path;
    }

    window.fetch = function (input, init) {
        const opts = init || {};
        const isInternal = opts && opts[INTERNAL_TAG] === true;
        if (!isInternal) {
            const url = typeof input === 'string' ? input : (input && input.url) || '';
            if (url.indexOf('/api/') !== -1) {
                const method = (opts.method || (typeof input === 'object' && input.method) || 'GET').toUpperCase();
                const key = dedupKey(method, url);
                if (!warnedKeys.has(key)) {
                    warnedKeys.add(key);
                    console.warn('[ApiUtils] Bypassed apiFetch/apiFetchMutate: ' + method + ' ' + url +
                        ' — route /api/ calls through window.ApiUtils so retry/spinner/debounce wiring stays consistent.');
                }
            }
        }
        return nativeFetch.call(window, input, init);
    };

    window.ApiUtils = { apiFetch: apiFetch, apiFetchMutate: apiFetchMutate };
})();
