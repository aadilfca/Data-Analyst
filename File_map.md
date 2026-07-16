# Project File Inventory

## Root Directory

| File | Purpose |
|------|---------|
| `CLAUDE.md` | Project overview, stack, architecture map, workflow rules, and complete status log. |
| `CLINE.md` | AI‑agent rule set – conventions, code‑format constraints, and communication style. |
| `database.py` | Database connection, query helpers, schema initialisation, audit logging, and seeding. |
| `main.py` | FastAPI app entry point; mounts static files, includes routers, and global exception handler. |
| `requirements.txt` | Python dependencies (FastAPI, Uvicorn, Pandas, Pydantic, OpenPyXL, python‑multipart). |
| `run.bat` | Windows batch script to start the Uvicorn server with the virtual environment. |

---

## Routers (`routers/`)

| File | Purpose |
|------|---------|
| `clients.py` | CRUD endpoints for client entities (create, read, update, soft‑delete, restore). |
| `dashboard.py` | Root route, health check, and dashboard statistics endpoint. |
| `data.py` | Data Bank endpoints – dataset summaries, preview rows, row history, availability check, and delete/restore. |
| `error_log.py` | Error log listing, detail view, resolution marking, and stats aggregation. |
| `import_export.py` | Excel upload, auto‑mapping, validation, change detection, and template download. |
| `settings.py` | Retrieve and update application‑wide settings. |
| `templates.py` | Master columns and template management (create, edit, delete, reorder columns). |
| `verification.py` | Cashbook identification, session creation, voucher/line grouping, validation, completion, and export. |

---

## Static Files (`static/`)

### HTML Pages

| File | Purpose |
|------|---------|
| `clients.html` | UI for managing clients (add, edit, deactivate, restore). |
| `client_settings.html` | Per-client voucher prefix configuration (Cash/Bank/Journal chip editors) reached via the ⚙️ icon on `clients.html`; also hosts a read-only explainer of the app's voucher-type identification logic. |
| `columns.html` | UI for managing master columns (add, edit, delete). |
| `databank.html` | Data Bank UI – date/template filters, availability table, preview modal, row history popover. |
| `error_dashboard.html` | Error log dashboard with filtering, resolution, and detail modal. |
| `index.html` | Main dashboard with module cards and quick stats. |
| `settings.html` | App settings UI (date format, amount grouping, precision, etc.). |
| `setup.html` | Setup hub linking to columns, templates, clients, error dashboard, and settings. |
| `templates.html` | Template management UI – create/edit templates, select columns, reorder with drag‑and‑drop. |
| `upload.html` | Excel upload UI – template selection, column mapping, mini preview, validation, and import. |
| `verification.html` | Verification module – cashbook selection, session view, voucher table, popovers, and supporting modals. |

### JavaScript Files

| File | Purpose |
|------|---------|
| `clients.js` | Client CRUD logic, table rendering, sorting, filtering, and modal handling. |
| `client_context.js` | Global client selection (dropdown/badge), localStorage persistence, and navbar updates. |
| `client_settings.js` | Loads/saves a client's Cash/Bank/Journal voucher prefixes via `routers/clients.py`'s voucher-prefixes API; add/remove chip handling and cross-category duplicate validation. |
| `columns.js` | Column CRUD logic, table rendering, category dropdown, and modal handling. |
| `databank.js` | Availability fetching, template preview, row history popover, export/delete actions. |
| `error_dashboard.js` | Error log loading, filtering, pagination, detail modal, resolution actions. |
| `script.js` | Dashboard stats loading and global keyboard shortcuts. |
| `settings.js` | Settings load/save, render settings groups, and status messages. |
| `setup.js` | Setup stats loading and backup/restore (export/import JSON). |
| `sortable.min.js` | Third‑party Sortable library for drag‑and‑drop column reordering. |
| `templates.js` | Template CRUD, column search/selection, preview modal, and drag‑and‑drop ordering. |
| `upload.js` | File upload, auto‑mapping, validation flow, import summary, and mini preview. |
| `utils.js` | Shared utilities – date formatting, amount formatting, and settings cache. |
| `verification.js` | Core verification logic – cashbook loading, session start, voucher rendering, navigation, validation, completion, and popovers. |

### CSS

| File | Purpose |
|------|---------|
| `style.css` | Global styles – navbar, layout, module cards, tables, modals, verification UI, and responsive tweaks. |

---

## Tests (`tests/`)

| File | Purpose |
|------|---------|
| `test_upload_validation_counts.py` | Unit test for the `count_new_and_existing_rows` helper. |
| `test_verification_indexes.py` | Unit test ensuring the index for cashbook lookups is created. |