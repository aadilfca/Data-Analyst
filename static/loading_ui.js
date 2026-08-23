/* Shared loading-UI utility (skeleton screens, spinner+message, inline field loading).
   Namespaced under window.LoadingUI rather than bare globals — client_context.js's own
   comments flag exactly this collision risk for a shared classic <script> (see
   error_dashboard.js's stale STORAGE_KEY / databank.js's shadowed getSelectedClientId()).
   Not yet wired into any page <script> tag — that's later phases. */
(function () {
    'use strict';

    // Per-element saved state (original innerHTML/disabled + active escalation timer),
    // keyed by element so concurrent independent loading states never collide and clean
    // up automatically if an element is removed from the DOM mid-load.
    const spinnerState = new WeakMap();
    const fieldState = new WeakMap();

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
    function showSpinner(target, message) {
        if (!target) return;
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

        saved.timeoutId = setTimeout(function () {
            msgSpan.textContent = (message || 'Loading…') + ' — still working…';
        }, 3000);

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
    function showFieldLoading(field) {
        if (!field || !field.parentElement) return;
        const parent = field.parentElement;
        const computedPosition = window.getComputedStyle(parent).position;
        const parentPositionSet = computedPosition === 'static';
        if (parentPositionSet) parent.style.position = 'relative';

        const spinnerEl = document.createElement('span');
        spinnerEl.className = 'ui-field-spinner';
        spinnerEl.innerHTML = '<span class="ui-spinner ui-spinner-sm"></span>';
        field.insertAdjacentElement('afterend', spinnerEl);

        const saved = {
            disabled: field.disabled,
            spinnerEl: spinnerEl,
            parentPositionSet: parentPositionSet,
            parent: parent
        };
        field.disabled = true;
        field.classList.add('ui-field-loading');
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
        if (saved.parentPositionSet) saved.parent.style.position = '';
        fieldState.delete(field);
    }

    window.LoadingUI = {
        showSkeleton: showSkeleton,
        hideSkeleton: hideSkeleton,
        showSpinner: showSpinner,
        hideSpinner: hideSpinner,
        showFieldLoading: showFieldLoading,
        hideFieldLoading: hideFieldLoading
    };
})();
