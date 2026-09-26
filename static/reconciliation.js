/*
 * Reports centre hub.
 *
 * Report-specific setup and generation will be added as focused workflows in later phases.
 * Existing backend routes remain available during the migration.
 */
(function () {
    'use strict';
    function setSelection(card) {
        const key = card.dataset.reportKey;
        if (key !== 'cashflows') return;

        const cards = Array.from(document.querySelectorAll('[data-report-key]'));
        cards.forEach((item) => item.classList.toggle('is-selected', item === card));
        const params = new URLSearchParams(window.location.search);
        params.set('view', 'reports');
        params.set('report', key);
        window.history.replaceState({}, '', window.location.pathname + '?' + params.toString());

    }

    function initialise() {
        const cards = Array.from(document.querySelectorAll('[data-report-key]'));
        cards.forEach((card) => {
            card.addEventListener('click', () => setSelection(card));
            card.addEventListener('keydown', (event) => {
                if (event.key === 'Enter' || event.key === ' ') {
                    event.preventDefault();
                    setSelection(card);
                }
            });
        });

        const requested = new URLSearchParams(window.location.search).get('report');
        const requestedCard = cards.find((card) => card.dataset.reportKey === requested);
        if (requestedCard) setSelection(requestedCard);
    }
    document.addEventListener('DOMContentLoaded', initialise);
}());
