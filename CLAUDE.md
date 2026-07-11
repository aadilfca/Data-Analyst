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

## Workflow rules
- Before editing, list files to be touched and wait for my confirmation
- Use Plan Mode for anything touching more than one file
- One phase at a time, stop for review after each

## Status log
- 2026-07-09: main.py NameError fixed
- 2026-07-09: clients module reviewed, no issues
[keep adding one line per session]


## Self-maintenance
After completing any phase, task, or bug fix:
- Add a one-line entry to the Status log with date + what changed
- If new files were created, add them to the Architecture/file map
- If a new convention or rule emerges from a mistake, add it under Conventions
Do this automatically at the end of the session without being asked.