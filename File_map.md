# File Map — Internal Audit App

This document lists all source files in the project, grouped by directory, with a brief description of each file’s purpose and role.

---

## Root Directory

| File | Purpose |
|------|---------|
| `CLAUDE.md` | Project‑level documentation for developers. Contains stack, architecture map, conventions, workflow rules, and an extensive status log of completed features and fixes. |
| `CLINE.md` | Rules for the AI agent (Cline). Defines file access, preferred code format (FIND/REPLACE), constraints, and communication style. |
| `database.py` | Core database module. Manages SQLite connection, query helpers (`execute_query`, `execute_write`), schema initialisation (`init_db`), seeding of default data (error codes, audit procedures, master columns, system template, app settings), audit logging, and per‑client voucher prefix storage. Also provides diff utilities and row history functions. |
| `main.py` | FastAPI application entry point. Mounts static files, includes all routers, and installs a global exception handler that logs errors to the database. Runs the Uvicorn server when executed directly. |
| `requirements.txt` | Python dependencies: FastAPI, Uvicorn, Pandas, Pydantic, OpenPyXL, python‑multipart. |
| `run.bat` | Windows batch script to start the Uvicorn server with the virtual environment (activates `venv` and runs `uvicorn main:app --reload`). |

---

## Routers (`routers/`)

| File | Purpose |
|------|---------|
| `clients.py` | Client management CRUD endpoints: create, read, update, soft‑delete, restore. New clients are auto‑seeded with default voucher prefixes. Also provides GET/PUT for a client's voucher prefix configuration (Cash Payment/Cash Receipt/Bank Payment/Bank Receipt/Journal — 5‑way, Payment/Receipt split explicitly) using the `client_voucher_prefixes` table. |
| `dashboard.py` | Root route, health check, and dashboard statistics endpoint (`/api/dashboard/stats`). |
| `data.py` | Data Bank endpoints. Retrieves datasets, previews rows (with pagination, search, amount filters, Sunday‑only filter), row revision history, availability checklist per template, and delete/restore operations for datasets and templates. |
| `error_log.py` | Error log management. Lists errors with filtering/pagination, fetches details, marks errors as resolved, and provides stats aggregation (total, pending, resolved, category breakdown). |
| `import_export.py` | Excel upload and import logic. Handles file upload, auto‑mapping of columns, validation, change detection (document‑level grouping with `document_group_id`), and import with support for new, updated, and deleted rows. Also downloads template Excel files. |
| `settings.py` | Application‑wide settings (key‑value store). GET all or a single setting, POST to update a setting. |
| `templates.py` | Master columns and template management. CRUD for columns and templates, reordering columns, removing column mappings, backup/export (JSON), and helper endpoints for upload preview. Includes a “close‑match” advisory endpoint for column name suggestions. |
| `verification.py` | Core verification module. Identifies cashbooks, starts verification sessions, provides paginated cashbook previews, voucher/line grouping, navigation (next/prev voucher), validation, completion, audit guidance lookup, supporting document search, and export of audited cashbooks to Excel. |

---

## Static Files (`static/`)

### HTML Pages

| File | Purpose |
|------|---------|
| `clients.html` | UI for managing clients. Lists clients with sorting, search, and actions (add, edit, deactivate, restore, and access client settings via cogwheel). |
| `client_settings.html` | Per‑client voucher prefix configuration page. Uses chip‑based editors for the 5 categories (Cash Payment/Cash Receipt/Bank Payment/Bank Receipt/Journal); also includes a read‑only explainer of how the app identifies voucher types. |
| `columns.html` | Manage master columns. Add, edit, soft‑delete, search, and sort columns. Includes a category dropdown. |
| `databank.html` | Client Data Bank. Displays template availability, date filters, preview modal with pagination and row history popovers, export to Excel, and delete/restore dataset actions. |
| `error_dashboard.html` | Error log dashboard. Shows stats, filterable table, detail modal, resolution actions, and pagination. |
| `index.html` | Main dashboard with module cards (Verification, Upload, Setup, Manage Clients, Data Bank, etc.) and quick stats. |
| `settings.html` | App settings UI. Renders settings groups (date format, amount grouping, decimal precision, default date range, materiality threshold) with save functionality. |
| `setup.html` | Setup hub linking to columns, templates, clients, error dashboard, and settings. Also provides backup/restore (export/import JSON). |
| `templates.html` | Template management. Create/edit templates by selecting columns from the master list, with live search, quick‑add column modal, drag‑and‑drop reordering, and preview. |
| `upload.html` | Excel upload interface. Template selection, sheet selector, column mapping table, mini preview, validation, and import with change‑detection summary popup. |
| `verification.html` | Verification module. Contains the cashbook selection popup, cashbook preview table, and the verification session view with voucher header, line rows, audit guidance popovers, supporting documents modal, procedure modal, and DFD placeholder modal. |

---

### JavaScript Files

| File | Purpose |
|------|---------|
| `clients.js` | Client CRUD logic: load, render table with sorting/filtering, open add/edit modal, save (with Save & New), delete/restore. |
| `client_context.js` | Global client context management. Handles client dropdown (dashboard) and badge (other pages), localStorage persistence, and provides functions to get selected client ID/name. Also manages module name and stats in the navbar. |
| `client_settings.js` | Loads/saves client voucher prefixes across the 5 categories. Implements chip‑based editors, add/remove prefixes, cross‑category duplicate validation, and save to API (bridges the 5 display category names to the API's Python‑identifier‑safe field names via `CATEGORIES`). |
| `columns.js` | Master column management: load, render table, add/edit modal, delete, search/filter, and category dropdown. |
| `databank.js` | Data Bank logic: fetch availability, render table, preview modal with pagination and filters, row history popover, export, and delete template data. |
| `error_dashboard.js` | Error log dashboard: load stats, fetch errors with filters, render table, open detail modal, resolve/unresolve, delete, and pagination. |
| `script.js` | Dashboard stats loading, keyboard shortcuts (R to refresh), click feedback for module cards, and global ESC key handling for modals. |
| `settings.js` | App settings: fetch settings, render groups, save changes, and status messages. |
| `setup.js` | Setup stats loading, backup export/import, and click feedback. |
| `sortable.min.js` | Third‑party Sortable library (v1.15.0) for drag‑and‑drop column reordering in templates. |
| `templates.js` | Template CRUD: load templates, render table, open create/edit modal, search and add columns, quick‑add column modal, drag‑and‑drop reordering, preview modal, and delete. |
| `upload.js` | Upload logic: template selection, file handling (drag‑and‑drop, click), auto‑mapping, mini preview, validation, import with change‑detection summary, and preview sheet with pagination. |
| `utils.js` | Shared utilities: date formatting, amount formatting (with grouping and decimal precision), and settings cache. |
| `verification.js` | Core verification frontend. Handles cashbook selection, session start, cashbook preview, voucher header rendering, line rendering, navigation (prev/next), validation, completion, audit guidance popover, procedure modal, DFD modal, transaction list popover, and progress updates. |

---

### CSS

| File | Purpose |
|------|---------|
| `style.css` | Global styles for the entire application. Includes navbar, layout, module cards, tables, modals, verification UI (voucher panel, line rows, popovers), and responsive tweaks. |

---

## Tests (`tests/`)

| File | Purpose |
|------|---------|
| `test_upload_validation_counts.py` | Unit test for the `count_new_and_existing_rows` helper in `import_export.py`. Verifies that new and existing rows are counted correctly. |
| `test_verification_indexes.py` | Unit test ensuring that the composite index on `client_data` (for cashbook lookups) is created by the database initialisation. |

---

## Notes

- **Static files** are served under `/static/` and rely on `client_context.js` for global client selection and navbar updates.
- **Routers** are mounted in `main.py` and each handles a specific domain (clients, verification, data, etc.).
- **Database** is SQLite with WAL mode; schema is managed by `init_db()` in `database.py`.
- **Uploads** are stored in the `uploads/` directory (created at runtime).
- **Change detection** uses the `document_group_id` and `change_detection_status` columns on `client_data`.
- **Voucher grouping** in verification uses the `document_group_id` computed from the first template column (lowest `sort_order`).
- **Client voucher prefixes** are stored per client in `client_voucher_prefixes` (5‑way: Cash Payment/Cash Receipt/Bank Payment/Bank Receipt/Journal) and are used by verification to identify voucher categories, with Payment vs Receipt stored explicitly rather than derived from debit/credit sign.

---

*This file map is maintained alongside the project. Update it when new files are added or when existing file roles change.*