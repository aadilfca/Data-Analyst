# Project: Internal Audit App

## Stack
FastAPI (Python) backend, HTML+CSS+JS frontend, SQLite

## About me
CA, non-CS developer. Explain tradeoffs in plain terms.

## Architecture (file map)
- routers/clients.py + static/clients.* → client management
- routers/dashboard.py + static/index.html, script.js → dashboard
- routers/data.py + static/databank.* → data import/columns
- routers/verification.py + static/verification.* → verification
- routers/error_log.py + static/error_dashboard.* → error logging
- routers/import_export.py + static/upload.* → import/export
- routers/settings.py + static/settings.* → settings
- routers/templates.py + static/templates.*, template_manager.js → templates
- database.py, main.py → shared core, DB schema, app entry

## Commands
- Run app: [your actual run command]
- Run tests: [if you have any]

## Conventions
- No inline event handlers (onclick, onload) in HTML
- No <script> inside HTML containers
- Meaningful comments on new/changed code
- Keep changes scoped — don't touch unrelated code
- Document grouping (get_document_group_id in routers/import_export.py) assumes the first template column (lowest sort_order) is a unique document key. Do not reorder template columns without checking this assumption holds.

## Workflow rules
- Before editing, list files to be touched and wait for my confirmation
- Use Plan Mode for anything touching more than one file
- One phase at a time, stop for review after each

## Status log
- 2026-07-09: main.py NameError fixed
- 2026-07-09: clients module reviewed, no issues
- 2026-07-12: Phase 3 done — hover ⚠️ history icon + popover added to Data Bank preview S.No column (static/databank.js, static/databank.html). Also fixed a pre-existing bug found during verification: get_data_audit_history() and /api/data/rows/{id}/history queried a non-existent "created_at" column instead of "changed_at" (database.py, routers/data.py) — this was blocking the Phase 1 history endpoint entirely.
- 2026-07-12: Phase 4 done — redesigned Import Summary popup (showImportSummary() in static/upload.js): removed run-on emoji sentences, restyled Change Detection Summary as a clean 4-tile stat block, kept View in Data Bank / Close buttons unchanged. No backend/data changes. Pending manual browser verification of the upload flow.
- 2026-07-13: Upload redesign Phase 1 — removed .module-header, rebuilt top bar as breadcrumb (Back · Template · Download Template · Sheet · badge · Go to Data Bank), relocated Download Template so it enables as soon as a template is chosen instead of after mapping, replaced inline onclick on the Data Bank shortcut with addEventListener (static/upload.html, static/upload.js).
- 2026-07-13: Upload flush-layout fix + Phase 2 done — zeroed body/.container outer padding on the Upload page so it sits flush like verification.html's .main-container.verification-active pattern; consolidated the overwrite note + Upload/Reset buttons into one .action-bar (was two stacked .mode-row/.btn-row divs), restyled the mapping table (zebra striping, uppercase section title) and moved the mini-preview toggle button's inline style into a .btn-mini-preview-toggle CSS class; removed dead CSS left over from an earlier mode-row/select/hint layout (static/upload.html). No JS logic changes — all element IDs and click handlers unchanged.
- 2026-07-13: Upload navbar-gap fix + Phase 3 done — found the remaining navbar-to-content gap was a margin-collapse artifact (.top-bar/.client-warning top margins leaking through .container, which has no top border/padding); fixed by dropping their top margin to 0 while keeping bottom/side margins. Also restyled .upload-area (typography, hover elevation) and #miniPreviewContainer (8px radius, uppercase header label) for visual consistency with the Phase 2 mapping table/action bar, and removed a duplicated "RIGHT PANEL" HTML comment (static/upload.html). Styling only — drag/drop, renderMiniPreview(), toggleMiniPreview(), closeMiniPreview() untouched.
- 2026-07-13: Verification card redesign Phase 1 (Layout Skeleton) done — renderTransaction() in static/verification.js now wraps the transaction-details table (Segment 1), a new empty placeholder (Segment 2, for Phase 2 tabs), and the procedures table (Segment 3, full width) in a new .transaction-grid CSS grid (static/style.css); validation-status-row and change-detection-banner moved to sit after the grid, ahead of the unchanged obs-comments-row, to match the confirmed final block order. No content, backend, or event-listener changes. Pending manual browser verification.
- 2026-07-13: Verification card redesign Phase 2 (Segment 2 Tab Shell) done — filled the Segment 2 placeholder in renderTransaction() (static/verification.js) with a DFD/AUDIT tab header and two empty panels (.segment2-tab-header/.segment2-tab-btn/.segment2-tab-panel styles added to static/style.css); AUDIT is active by default. Tab switching is a new client-side-only click listener (no API calls, no persistence across transaction navigation — state resets to AUDIT on each renderTransaction() call). Segment 1/3 untouched.
[keep adding one line per session]


## Self-maintenance
After completing any phase, task, or bug fix:
- Add a one-line entry to the Status log with date + what changed
- If new files were created, add them to the Architecture/file map
- If a new convention or rule emerges from a mistake, add it under Conventions
Do this automatically at the end of the session without being asked.