/* Shared loading-UI utility (skeleton screens, spinner+message, inline field loading).
   Namespaced under window.LoadingUI rather than bare globals — client_context.js's own
   comments flag exactly this collision risk for a shared classic <script> (see
   error_dashboard.js's stale STORAGE_KEY / databank.js's shadowed getSelectedClientId()).
   The global cursor is currently piloted on the dashboard; remaining pages are later phases. */
(function () {
    'use strict';

    // Per-element saved state (original innerHTML/disabled + active escalation timer),
    // keyed by element so concurrent independent loading states never collide and clean
    // up automatically if an element is removed from the DOM mid-load.
    const spinnerState = new WeakMap();
    const fieldState = new WeakMap();
    const progressState = new WeakMap();
    const badgeState = new WeakMap();
    const globalLoadingTokens = new Set();
    let nextGlobalLoadingToken = 1;
    let originalDocumentCursor = '';

    // Ambient progress cursor. The caller owns the returned token and must pass the same
    // token to stopGlobalLoading; repeated or stale stops are harmless. The standard
    // `progress` cursor keeps the interface usable, unlike the browser's `wait` cursor.
    function startGlobalLoading() {
        const token = { id: nextGlobalLoadingToken++ };
        if (globalLoadingTokens.size === 0 && document.documentElement) {
            originalDocumentCursor = document.documentElement.style.cursor;
            document.documentElement.style.cursor = 'progress';
        }
        globalLoadingTokens.add(token);
        return token;
    }

    function stopGlobalLoading(token) {
        if (!token || !globalLoadingTokens.has(token)) return;
        globalLoadingTokens.delete(token);
        if (globalLoadingTokens.size === 0 && document.documentElement) {
            document.documentElement.style.cursor = originalDocumentCursor;
            originalDocumentCursor = '';
        }
    }

    function isTableContainer(container) {
        const tag = container.tagName;
        return tag === 'TBODY' || tag === 'TABLE' || tag === 'TR';
    }

    // rows shimmering placeholders, rendered immediately (no threshold delay) — the
    // caller's real render simply overwrites container.innerHTML once data arrives.
    function showSkeleton(container, rows, options) {
        if (!container) return;
        const count = rows || 3;
        const opts = options || {};
        if (isTableContainer(container)) {
            const colspan = opts.columns || 1;
            let html = '';
            for (let i = 0; i < count; i++) {
                html += '<tr class="ui-skeleton-row-table"><td colspan="' + colspan + '" class="ui-skeleton-cell">' +
                    '<span class="ui-skeleton-bar"></span></td></tr>';
            }
            container.innerHTML = html;
        } else {
            let html = '';
            for (let i = 0; i < count; i++) {
                html += '<div class="ui-skeleton-row"><span class="ui-skeleton-bar"></span></div>';
            }
            container.innerHTML = html;
        }
    }

    function hideSkeleton(container) {
        if (!container) return;
        container.innerHTML = '';
    }

    // Button/action spinner. Only the message text escalates after 3s — the spinner
    // itself is shown instantly and never changes appearance.
    // options.escalate (default true) can be set false to skip the 3s escalation entirely,
    // keeping the message capped at its original text — for a target too narrow to hold the
    // longer escalated message (e.g. a table-cell badge), rather than a button with room to spare.
    function showSpinner(target, message, options) {
        if (!target) return;
        const escalate = !options || options.escalate !== false;
        const isButton = target.tagName === 'BUTTON' || target.disabled !== undefined;
        const saved = {
            html: target.innerHTML,
            disabled: isButton ? target.disabled : undefined,
            timeoutId: null
        };

        const msgSpan = document.createElement('span');
        msgSpan.className = 'ui-btn-spinner-message';
        msgSpan.textContent = message || 'Loading…';

        const wrapper = document.createElement('span');
        wrapper.className = 'ui-btn-spinner-content';
        const spinnerEl = document.createElement('span');
        spinnerEl.className = 'ui-spinner';
        wrapper.appendChild(spinnerEl);
        wrapper.appendChild(msgSpan);

        target.innerHTML = '';
        target.appendChild(wrapper);
        if (isButton) target.disabled = true;

        if (escalate) {
            saved.timeoutId = setTimeout(function () {
                msgSpan.textContent = (message || 'Loading…') + ' — still working…';
            }, 3000);
        }

        spinnerState.set(target, saved);
    }

    function hideSpinner(target) {
        if (!target) return;
        const saved = spinnerState.get(target);
        if (!saved) return;
        if (saved.timeoutId) clearTimeout(saved.timeoutId);
        target.innerHTML = saved.html;
        if (saved.disabled !== undefined) target.disabled = saved.disabled;
        spinnerState.delete(target);
    }

    // Inline field loading (dropdowns/inputs) — disables the field and drops a small
    // spinner right after it in the DOM, positioned against the nearest ancestor that
    // already establishes a positioning context (or the field's own parent, made
    // relative only if it wasn't already — reverted on hide, never clobbering the
    // page's own layout CSS).
    // options.prominent (e.g. a dropdown whose stale content would otherwise mislead the user)
    // swaps the small after-field spinner for a dimmed overlay + larger centered spinner covering
    // the field itself, instead of a third primitive.
    function showFieldLoading(field, options) {
        if (!field || !field.parentElement) return;
        const prominent = !!(options && options.prominent);
        const parent = field.parentElement;
        const computedPosition = window.getComputedStyle(parent).position;
        const parentPositionSet = computedPosition === 'static';
        if (parentPositionSet) parent.style.position = 'relative';

        const spinnerEl = document.createElement('span');
        if (prominent) {
            spinnerEl.className = 'ui-field-overlay-prominent';
            spinnerEl.innerHTML = '<span class="ui-spinner ui-spinner-lg"></span>';
        } else {
            spinnerEl.className = 'ui-field-spinner';
            spinnerEl.innerHTML = '<span class="ui-spinner ui-spinner-sm"></span>';
        }
        field.insertAdjacentElement('afterend', spinnerEl);

        const saved = {
            disabled: field.disabled,
            spinnerEl: spinnerEl,
            parentPositionSet: parentPositionSet,
            parent: parent,
            prominent: prominent
        };
        field.disabled = true;
        field.classList.add('ui-field-loading');
        if (prominent) field.classList.add('ui-field-loading-prominent');
        fieldState.set(field, saved);
    }

    function hideFieldLoading(field) {
        if (!field) return;
        const saved = fieldState.get(field);
        if (!saved) return;
        if (saved.spinnerEl && saved.spinnerEl.parentNode) {
            saved.spinnerEl.parentNode.removeChild(saved.spinnerEl);
        }
        field.disabled = saved.disabled;
        field.classList.remove('ui-field-loading');
        if (saved.prominent) field.classList.remove('ui-field-loading-prominent');
        if (saved.parentPositionSet) saved.parent.style.position = '';
        fieldState.delete(field);
    }

    // Progress bar. Three modes, since no endpoint currently reports real x/x progress for most
    // callers this will serve (see loading_ui.js's own header comment on wiring being a later
    // phase): 'determinate' (caller drives it via updateProgressBar), 'simulated' (elapsed-time
    // estimate — no real data, so the fill is deliberately capped short of 100% until hideProgressBar
    // is actually called), and 'indeterminate' (a labelled sliding bar, no count — e.g. "Saving…").
    // Save/replace/restore follows the same pattern as showSpinner/hideSpinner above.
    function showProgressBar(target, options) {
        if (!target) return;
        const opts = options || {};
        const mode = opts.mode || 'indeterminate';
        const isButton = target.tagName === 'BUTTON' || target.disabled !== undefined;

        const saved = {
            html: target.innerHTML,
            disabled: isButton ? target.disabled : undefined,
            mode: mode,
            intervalId: null
        };

        const wrapper = document.createElement('div');
        wrapper.className = 'ui-progress-bar-wrapper' + (mode === 'indeterminate' ? ' ui-progress-bar-indeterminate' : '');

        const labelEl = document.createElement('span');
        labelEl.className = 'ui-progress-bar-label';
        labelEl.textContent = opts.message || 'Loading…';
        wrapper.appendChild(labelEl);

        if (mode === 'determinate') {
            const counterEl = document.createElement('span');
            counterEl.className = 'ui-progress-bar-counter';
            counterEl.textContent = '0 / ' + (opts.total || 0);
            labelEl.appendChild(counterEl);
            saved.counterEl = counterEl;
        }

        const track = document.createElement('div');
        track.className = 'ui-progress-bar-track';
        const fill = document.createElement('div');
        fill.className = 'ui-progress-bar-fill';
        fill.style.width = '0%';
        track.appendChild(fill);
        wrapper.appendChild(track);

        target.innerHTML = '';
        target.appendChild(wrapper);
        if (isButton) target.disabled = true;

        saved.fillEl = fill;
        progressState.set(target, saved);

        if (mode === 'simulated') {
            const startTime = Date.now();
            saved.intervalId = setInterval(function () {
                const elapsed = Date.now() - startTime;
                const pct = 90 * (1 - Math.exp(-elapsed / 2000));
                fill.style.width = pct + '%';
            }, 150);
        }
    }

    // Determinate mode only — no-op for simulated/indeterminate or an element with no active
    // progress bar, so a stray call from a caller that raced hideProgressBar can't throw.
    function updateProgressBar(target, current, total) {
        if (!target) return;
        const saved = progressState.get(target);
        if (!saved || saved.mode !== 'determinate') return;
        const pct = total > 0 ? Math.min(100, (current / total) * 100) : 0;
        saved.fillEl.style.width = pct + '%';
        if (saved.counterEl) saved.counterEl.textContent = current + ' / ' + total;
    }

    function hideProgressBar(target) {
        if (!target) return;
        const saved = progressState.get(target);
        if (!saved) return;
        if (saved.intervalId) clearInterval(saved.intervalId);
        target.innerHTML = saved.html;
        if (saved.disabled !== undefined) target.disabled = saved.disabled;
        progressState.delete(target);
    }

    // Persistent badge — unlike the primitives above, this one describes ongoing state (e.g. "3
    // rows still loading") rather than a single transient action, so it stays until the caller
    // explicitly hides it. Repeated show calls on the same container update the existing badge
    // in place rather than stacking duplicates.
    function showBadge(container, text, options) {
        if (!container) return;
        const opts = options || {};
        let badgeEl = badgeState.get(container);
        if (!badgeEl) {
            badgeEl = document.createElement('span');
            container.appendChild(badgeEl);
            badgeState.set(container, badgeEl);
        }
        badgeEl.className = 'ui-badge' + (opts.tone === 'warning' ? ' ui-badge-warning' : '');
        badgeEl.textContent = text || '';
    }

    function updateBadge(container, text) {
        if (!container) return;
        const badgeEl = badgeState.get(container);
        if (!badgeEl) return;
        badgeEl.textContent = text || '';
    }

    function hideBadge(container) {
        if (!container) return;
        const badgeEl = badgeState.get(container);
        if (!badgeEl) return;
        if (badgeEl.parentNode) badgeEl.parentNode.removeChild(badgeEl);
        badgeState.delete(container);
    }

    window.LoadingUI = {
        startGlobalLoading: startGlobalLoading,
        stopGlobalLoading: stopGlobalLoading,
        showSkeleton: showSkeleton,
        hideSkeleton: hideSkeleton,
        showSpinner: showSpinner,
        hideSpinner: hideSpinner,
        showFieldLoading: showFieldLoading,
        hideFieldLoading: hideFieldLoading,
        showProgressBar: showProgressBar,
        updateProgressBar: updateProgressBar,
        hideProgressBar: hideProgressBar,
        showBadge: showBadge,
        updateBadge: updateBadge,
        hideBadge: hideBadge
    };
})();
