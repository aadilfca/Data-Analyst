# File Map — Internal Audit App

This document lists all source files in the project, grouped by directory, with a brief description of each file's *current* purpose and role. This is a snapshot of what exists today — it does not track build history or phase status; see `CHANGELOG_ARCHIVE.md` for that.

---

## Root Directory

| File | Purpose |
|------|---------|
| `CLAUDE.md` | Project-level context for Claude Code: stack, architecture map, conventions, workflow rules, and a one-line-per-phase status log. Read automatically every session. |
| `CHANGELOG_ARCHIVE.md` | Detailed forensic write-ups of past phases (verification steps, test output) — only consulted when a phase touches an already-logged file/pattern. |
| `CLINE.md` | Rules file for a separate AI tool, Cline (FIND/REPLACE code format, file-access/communication style) — not read by Claude Code. Appears to predate the current CLAUDE.md-driven workflow: it tells the agent to auto-determine affected files without asking, which conflicts with this project's current two-hard-stops rule in CLAUDE.md's Workflow rules. Kept only for whichever sessions still use Cline directly. |
| `database.py` | Core database module. SQLite connection helpers with lock-retry (`get_db`, `execute_query`, `execute_write`, `get_raw_conn`), full schema (`init_db` — creates/upgrades every table, including the Audit Rules module's `audit_rule_*` tables, the Trial Balance module's `trial_balance_uploads`/`trial_balance_accounts`, `account_category_assignments`, the Voucher Supporting-Document Linking tables `voucher_line_documents`/`voucher_line_procedure_checks`/`voucher_line_observations`, and the Validation Rules module's schema foundation — `approval_required` on `audit_rule_category_documents`, `matrix_version` on `audit_rule_categories`, the new `signature_specimens` table, `signature_exception` on `voucher_line_documents`, and `cleared_status`/`matrix_version_applied` on `verification_details`), seed data, audit-logging helpers (`log_audit`, `log_data_audit`, `get_data_audit_history`, `diff_row_data`), and per-client voucher prefix storage (5-way category scheme). |
| `main.py` | FastAPI application entry point. Mounts `/static`, registers all 10 routers (dashboard, verification, import_export, templates, clients, data, error_log, settings, audit_rules, trial_balance), and installs a global exception handler that logs unhandled errors to `error_log`. Runs Uvicorn when executed directly. |
| `requirements.txt` | Python dependencies: FastAPI, Uvicorn, Pandas, Pydantic, OpenPyXL, python-multipart, xlrd (legacy `.xls` reads for Trial Balance). |
| `run.bat` | Windows launcher — activates `venv` and starts Uvicorn on port 5000. |

---

## Routers (`routers/`)

| File | Purpose |
|------|---------|
| `clients.py` | Client CRUD (create/list/get/update/soft-delete/restore), unique `client_code` enforcement, and GET/PUT for a client's voucher-prefix configuration (5-way: Cash Payment/Cash Receipt/Bank Payment/Bank Receipt/Journal, `client_voucher_prefixes` table) — new clients are auto-seeded with the 5 defaults. |
| `dashboard.py` | Root/health endpoints and the main dashboard's stats (total uploads, pending verifications, total records, last upload). |
| `data.py` | Client Data Bank backend: dataset listing, paginated row preview (`get_preview_rows` — search/amount/Sunday/data-quality filters, per-row voucher-line assignment flags, document grouping with subtotal rows), per-upload row listing, dataset/template soft-delete + restore, availability checklist, the per-day coverage calendar (`get_availability_calendar` — Sundays + firm-wide holidays), and the row revision history endpoint. |
| `error_log.py` | CRUD and stats for the `error_log` table populated by the global exception handler — list with filters, get one, mark resolved, delete, stats summary. |
| `import_export.py` | The upload/import flow: raw `.xlsx`/`.xls` upload, auto-mapping Excel headers to template columns, flexible date-format parsing (`parse_date_flexible`/`classify_date_cell`), row exclusion and force-include overrides, row-level rejection on blank Document ID/Amount/Date (`get_critical_field_columns`/`find_critical_blank_reasons`/`compute_data_quality_flag`), duplicate checking, validate-only, and the main validate+import endpoint with change detection (`compare_transactions` — groups by `document_group_id`, classifies unchanged/changed/new/missing). Also serves the downloadable template Excel and the Save Column Mapping feature (save/load/clear a client+template's column mapping). |
| `settings.py` | Get/update global app settings (date format, amount grouping, decimal precision, default date range, materiality threshold) stored as key-value pairs, plus full CRUD for the firm-wide Holiday list (`GET/POST /api/settings/holidays`, `PUT/DELETE /api/settings/holidays/{id}` — date/label/repeats-yearly, registered above the generic `/{key}` catch-all route). |
| `templates.py` | Master column CRUD (case-insensitive duplicate check, close-match advisory endpoint), template CRUD (with pivot-table column mappings, `sort_order`, quick-add-column support), template reordering, and backup/export + restore of the entire setup. |
| `verification.py` | Cash/Bank Verification module backend: per-client voucher-category detection (5-way scheme, `match_voucher_category`/`voucher_type_label`), cashbook/bankbook discovery, starting a verification session (voucher/line grouping via `document_group_id`), stepping through vouchers (`get_next_transaction`, `get_voucher_nav` — Prev/Next by whole voucher), saving/validating/completing lines, cashbook preview, the full audited-cashbook export, Audit Rules Category-based guidance lookup (`get_audit_guidance`, replacing the old hardcoded procedure library), the Voucher Supporting-Document Linking feature (assigning Data Bank rows to voucher lines as evidence, audit-procedure checklists, per-document observation capture/sync/roll-up into `verification_details.observations`), the Verification ↔ Assign Categories link (`category-for-account` lookup feeding the per-line Type field), the Validation Rules module's status engine/frontend endpoints (`compute_and_persist_cleared_status`, `voucher_approval`, `set_signature_exception`), and the Account Head recommendation lookup (`suggest-account-head`, fuzzy-matches a line's description against the client's leaf Trial Balance accounts via `rapidfuzz`). |
| `audit_rules.py` | Audit Rules module backend: full CRUD (soft-delete/restore) for the three reusable rule-building entities — Documents (with auth roles, cross-check-eligible fields, and a `template_id` link to the Data Bank template their evidence is searched in), Cross-checks (comparison type + ordered list of Documents), and Categories (bundling Documents + Cross-checks, with a locked containment rule). Categories are what `verification.py`'s Transaction Type / audit guidance now sources from. Also hosts the Validation Rules module's backend (Phase 2 of 7): per-document `approval_required` toggle within a Category (`PUT /api/audit-rules/categories/{id}/documents/{doc_id}/approval`), automatic `matrix_version` bump (`bump_category_matrix_version()`, fired on document-membership change or an actual approval-flag change), and a 5-endpoint (no soft-delete — table has no `is_active`) CRUD for `signature_specimens` (client+document+role → expected signatory name). |
| `trial_balance.py` | Trial Balance module backend. `POST /api/trial-balance/upload` reads a client's TB Excel file via an explicit column-mapping payload (8 standard fields: Code/Name/Opening Dr/Cr/Period Dr/Cr/Closing Dr/Cr, plus optional Type), classifies every account as parent/leaf from its code hierarchy, supports period-dated uploads with overwrite-conflict detection, and auto-carries forward Category assignments by account code. Also provides mapping suggestions, a downloadable standard template, per-upload account listing/summary/manual leaf-parent override, a list of all uploads for a client (Available Trial Balances), and the Category-assignment endpoints (`latest-accounts`/`assign-category`, smart-fill or force-apply cascade) that back the Assign Categories screen. |

---

## Static Files (`static/`)

### HTML Pages

| File | Purpose |
|------|---------|
| `index.html` | Main dashboard — module cards grouped into Audit work / Client data / Administration sections, plus quick stats. |
| `clients.html` | Manage Clients — sortable/searchable table, add/edit modal, deactivate/restore, and a ⚙️ cogwheel link to each client's Client Settings page. |
| `client_settings.html` | Per-client voucher prefix configuration — chip-list editors for the 5 categories (Cash Payment/Cash Receipt/Bank Payment/Bank Receipt/Journal), plus a static read-only "how the app identifies voucher types" explainer. |
| `columns.html` | Manage master columns — sortable/searchable table, add/edit modal with category dropdown, soft-delete. |
| `templates.html` | Manage Templates — create/edit templates by searching/selecting master columns (with a quick-add-column modal), drag-to-reorder selected columns, preview modal, soft-delete. |
| `upload.html` | Upload Client Data — template/sheet selection, Date Delimiter/Order format popover with a live sample-date preview, auto-mapping table with Save/Clear Saved Mapping, Row Selection grid (exclude rows, auto-flag heuristic, blank-critical-field rejection with an "Import anyway" override), full-sheet preview, Validate → Upload flow, and the post-upload Import Summary popup. |
| `databank.html` | Client Data Bank — "Your Datasets" table (template/date range/document count/coverage/actions), a per-template Coverage Calendar popup (present/missing/Sunday/holiday, missing-dates report), a shared date-range popover feeding both the calendar and the preview, and a preview modal (paginated, searchable, amount/data-quality filters, document grouping with subtotal rows, row revision history popover). |
| `error_dashboard.html` | Error Dashboard — stats cards, filterable/paginated error table, detail modal, resolve/delete actions. |
| `settings.html` | App Settings — form controls rendered from a fixed settings definition, plus a firm-wide Holiday list section (add/view/delete). |
| `setup.html` | Setup hub — links to Columns/Templates/Error Dashboard/Settings, quick stats, and Backup & Restore (export/import all setup data as JSON). |
| `verification.html` | Cash/Bank Verification module — hub entry picker (live Cash/Bank voucher tiles + inert "coming soon" tiles), cashbook/bankbook selection popup with a book selector on the preview screen, cashbook preview page, and the verification session view: voucher header with Prev/Next/search-popover navigation, a resizable voucher-line grid (Sr no./Account Code/Type/Account Name/Amount/Supporting/Observations/Status columns), the Supporting-Document tabbed modal (search/browse/link evidence, audit-procedure checklist, observation capture), and a Client Data Links card strip generated dynamically from active templates. |
| `audit_rules.html` | Audit Rules module — three-pane shell (entity-type rail / list panel / detail edit panel) for all four entity types (Documents, Cross-checks, Categories — each with soft-delete/restore — and Signature Specimens, a hard-delete-only client-scoped entity), plus the Assign Categories full-page view (a client's latest parsed Trial Balance accounts with a per-row Category dropdown, smart-fill/force-apply cascade) reachable both from here and via a deep link from Verification's per-line Type field. Category detail also has a per-document Approval Required checklist and a read-only Matrix version indicator (Validation Rules module, Phase 3 of 7). Reached from the dashboard's own Audit Rules card. |
| `trial_balance.html` | Trial Balance module — client-context-driven upload form (Period Start/End/Type, file picker, column-mapping window with live preview and a downloadable standard-template escape hatch, overwrite-conflict confirmation), a summary tile + per-account Review Classifications table with manual leaf/parent override, and an Available Trial Balances view (dropdown of past uploads, read-only account table, delete). Reachable from the dashboard's Trial Balance card. |
| `template-viewer.html` | Shared, reusable template-data viewer, parameterised by `?template=N` — date range + search toolbar over one template's Data Bank rows, reached from Verification's Client Data Links card strip. |

---

### JavaScript Files

| File | Purpose |
|------|---------|
| `client_context.js` | Global client-selection state shared by every page — navbar client dropdown/badge, `localStorage` persistence, client-selection enforcement, `setNavModule`/`setNavStats`, the global quick-navigation `⊞` dropdown + search field (`NAV_MANIFEST`-driven), and the back-arrow/home-icon navigation (`getBackTarget`, `setBackOverride` for pages with JS-driven in-page views). |
| `utils.js` | Shared formatting helpers — `formatDate` and `formatAmount`, both driven by cached `/api/settings` data. |
| `script.js` | Dashboard stats loading, keyboard shortcuts, `MODULE_CARD_DESTINATIONS`-driven card navigation, and click-feedback animation (excluding inert "coming soon" cards). |
| `clients.js` | Client CRUD: load, render table with sorting/filtering, add/edit modal, delete/restore, and the ⚙️ Client Settings link. |
| `client_settings.js` | Loads/saves a client's 5-category voucher prefixes via chip-list editors, cross-category duplicate validation. |
| `columns.js` | Master column management: load, render table, add/edit modal, delete, search/filter. |
| `templates.js` | Template CRUD: load, render table, create/edit modal (search-and-add-columns with a quick-add-column modal), drag-and-drop reordering (`sortable.min.js`), preview modal, delete. |
| `upload.js` | Upload logic: template/sheet selection, date-format popover + live preview, auto-mapping (with Save/Clear Saved Mapping), Row Selection grid (exclusion, auto-flag, blank-critical-field rejection + override), full-sheet preview, validate/import, session-state restore across navigation, Import Summary popup. |
| `databank.js` | Data Bank logic: fetch availability, render "Your Datasets," per-template Coverage Calendar popup + missing-dates report, shared date-range popover, preview modal (pagination/search/filters/document grouping/row history popover), export, delete. |
| `error_dashboard.js` | Error log dashboard: load stats, fetch errors with filters, render table, detail modal, resolve/delete, pagination. |
| `settings.js` | App settings: fetch/render/save settings groups, plus the firm-wide Holiday list (add/render/delete). |
| `setup.js` | Setup hub stats, backup export/import, click feedback. |
| `verification.js` | Verification frontend: cashbook/bankbook discovery and session start, voucher-line grid rendering and navigation, the Supporting-Document tabbed modal (search/browse/assign/unassign evidence, audit-procedure checklist, observation capture/save), the Voucher-Change popover, the voucher-list search popover, Assign-Categories deep-link auto-fill for the Type field, and hard-refresh URL/view-state persistence. |
| `audit_rules.js` | Audit Rules module frontend: three-pane rail/list/detail for Documents, Cross-checks, Categories, and Signature Specimens (each fully duplicated per entity type, not shared — Signature Specimens is client-scoped and hard-delete-only, no is_active), plus the Assign Categories full-page view (smart-fill/force-apply Category assignment against a client's latest parsed Trial Balance, with deep-link row highlighting from Verification). Category detail also drives a live per-document Approval Required toggle (`PUT .../categories/{id}/documents/{doc_id}/approval`) and displays the Category's `matrix_version` (Validation Rules module, Phase 3 of 7). |
| `trial_balance.js` | Trial Balance module frontend: period-dated upload with column-mapping window and overwrite-conflict confirmation, summary tiles + Review Classifications table with override toggle, and the Available Trial Balances view. |
| `template-viewer.js` | Template Viewer frontend: resolves template config live from `GET /api/templates`, fetches/searches one template's Data Bank rows over a date range. |
| `sortable.min.js` | Third-party library (SortableJS) used for drag-to-reorder column lists in Templates. |

---

### CSS

| File | Purpose |
|------|---------|
| `style.css` | All application styling — navbar (incl. quick-navigation menu), module grid, tables, modals (each modal's `.modal-content`/`.modal-header` scoped per-id, not shared base classes), the Upload page's grids/popovers, the Data Bank's datasets table/calendar/preview, and the Verification module's voucher-line grid, Supporting-Document modal, and popovers. |

---

## Tests (`tests/`)

| File | Purpose |
|------|---------|
| `test_upload_validation_counts.py` | Verifies `count_new_and_existing_rows` correctly distinguishes new vs. already-imported rows for the same client/template. |
| `test_verification_indexes.py` | Verifies that `init_db()` creates the composite index needed for fast cashbook lookups on `client_data`. |

---

## Notes

- **Static files** are served under `/static/` and rely on `client_context.js` for global client selection and navbar behavior on every page.
- **Routers** are mounted in `main.py`; each handles one domain (clients, verification, data, audit_rules, trial_balance, etc.).
- **Database** is SQLite; schema is fully managed by `init_db()` in `database.py`, including idempotent migration/upgrade blocks for tables that gained columns after going live.
- **Uploads** are stored in the `uploads/` directory (created at runtime).
- **Change detection** uses `document_group_id`/`change_detection_status` on `client_data`; document grouping everywhere in the app assumes the first template column (lowest `sort_order`) is a unique document key.
- **Voucher category detection** uses the 5-way `client_voucher_prefixes` scheme (Cash Payment/Cash Receipt/Bank Payment/Bank Receipt/Journal), storing Payment vs Receipt explicitly rather than deriving it from debit/credit sign.

---

*This file map is maintained alongside the project — updated whenever a file is added, removed, or repurposed. Full build history and verification detail live in `CHANGELOG_ARCHIVE.md`, not here.*
