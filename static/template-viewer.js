// Template Viewer page shell (Phase 2 of 5).
//
// This phase is deliberately minimal: it only reads the `?template=` query param to fill in
// a placeholder title/subtitle. No API calls, no per-template column config, no data fetching,
// and no event listeners on the date range/search/Fetch controls — those are wired in later
// phases (Phase 3: per-template config + data fetch; Phase 4: search across all columns).

window.onload = function () {
    const titleEl = document.getElementById('templateViewerTitle');
    const subtitleEl = document.getElementById('templateViewerSubtitle');

    const params = new URLSearchParams(window.location.search);
    const templateParam = params.get('template');
    const templateId = Number(templateParam);

    if (templateParam && Number.isInteger(templateId) && templateId > 0) {
        // Placeholder text only — resolving the real template name/columns is Phase 3's job.
        titleEl.textContent = `📄 Template Viewer — Template #${templateId}`;
        subtitleEl.textContent = 'Full template details will load in a later phase.';
    } else {
        // Minimal guard against a missing/invalid param, not real per-template validation.
        titleEl.textContent = '📄 Template Viewer';
        subtitleEl.textContent = 'No template specified — open this page via a template link from Verification.';
    }
};
