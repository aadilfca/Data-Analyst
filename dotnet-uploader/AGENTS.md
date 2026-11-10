# .NET Application Migration — Codex Plan

## Purpose and status

Migrate the complete Python application backend to ASP.NET Core/C#, retaining Python for financial report calculations and report generation. ASP.NET Core hosts the existing HTML/CSS/JavaScript and supplies report data to Python. The folder name remains `dotnet-uploader/`; no C# project exists yet.

Plan dated 2026-11-10. P01 and P02 are accepted; see [MIGRATION_BASELINE.md](MIGRATION_BASELINE.md), [migration-baseline.json](migration-baseline.json), and [MIGRATION_CONTRACTS.md](MIGRATION_CONTRACTS.md). P03 scope preparation is next; P04–P63 remain pending. Each implementation phase still requires file/approach approval. The previous upload-only scope is superseded. Preserve the working Python app throughout staged replacement.

## Agreed technology direction

- **Runtime/API:** .NET 10 LTS and ASP.NET Core Web API, organized as feature-oriented controllers and service classes in one modular service.
- **Frontend:** retain existing HTML, CSS, JavaScript, navigation, and API behavior while ASP.NET Core takes over hosting and backend requests.
- **Database:** use the existing Supabase PostgreSQL database. Use one configured `NpgsqlDataSource` and parameterized Npgsql/Dapper SQL for explicit transaction control. The C# service must not connect to SQLite.
- **Data contract:** `client_data` remains the only current transaction store; keep raw values and report-required derived fields together. `client_data_history` retains prior changed/deleted row snapshots. Do not add durable full-row transaction copies.
- **Capacity:** Aadil selected the 500 MB Free-plan ceiling; [P02 C01](MIGRATION_CONTRACTS.md) defines projected-total checks. Existing Python/root-guide 400 MB rules await separately approved alignment.
- **Excel:** evaluate ExcelDataReader's row reader for `.xls` and `.xlsx`. Confirm current date, formula-result, Excel-error, source-order, mapping, and Footing behavior against representative workbooks before locking the library.
- **Schema:** use reviewed, versioned PostgreSQL SQL migrations for approved additive changes. Keep one schema owner for each change; the new service must not run unreviewed schema changes at startup.
- **Long uploads:** use durable PostgreSQL job state with a .NET worker. Persist owner, stage, progress, attempts, and recovery state; make every import step safe to retry. An in-memory queue alone is not durable.
- **Staged workbooks:** if jobs must survive a restart or deploy, use private, short-retention object storage. Never put client workbooks or database backups in Git. Render's service filesystem is ephemeral by default.
- **Authentication:** initially preserve existing `users`/`sessions`, password-hash compatibility, and selected-client behavior. Enforce server-side authorization on every protected operation; use secure, HttpOnly, SameSite cookies and CSRF protection for writes.
- **Hosting and operations:** Docker deployment is suitable for Render. Keep the service near Supabase; Singapore is Render's listed Asian region. Confirm worker availability and monthly cost before production hosting.
- **Verification:** use xUnit and ASP.NET Core `WebApplicationFactory` with a dedicated isolated PostgreSQL database. Compare against golden workbooks and Python report outputs; never rehearse imports against live client data.

## Ownership and report boundary

- .NET owns authentication, users, clients, settings, uploads, database retrieval/writes, Trial Balance, assignments, Audit Rules, Verification, Data Bank, backups, and public API/job/download coordination.
- Python retains Cashflows, Financial Statements, Notes, reconciliation calculations, report-specific transformations, and financial workbook generation. Confirm the exact function boundary in P02; files currently mix these responsibilities.
- Report-related configuration and database queries move to .NET. Python reporting must eventually receive explicit inputs without importing application routers or database helpers. Preserve proven SQL selection/aggregation semantics during extraction.
- Report requests flow from browser to .NET, then to a private Python report service. .NET supplies authorized inputs containing client, period, accounts, configuration, source revision, and contract version; Python returns results or artifacts.
- Read all report inputs from a consistent snapshot. Stream large inputs or use private expiring artifacts with completeness/hash checks; preserve decimal precision, dates, nulls, ordering, and line multiplicity. No durable transaction mirrors.
- .NET owns report job access and public progress/download APIs; Python owns calculation/rendering execution. Existing Python export jobs use local files and OS locks, so cross-host coordination requires an explicit replacement design.
- Give each production operation/scope one active writer during coexistence. Use isolated databases for comparison imports; route a module only after acceptance and preserve rollback compatibility.

## Phased implementation plan

Numbers describe the default sequence. P02 records dependencies; subsequent file lists must identify affected callers and prerequisites. Split a phase before implementation if it cannot be reviewed and tested as one bounded change. Each acceptance condition supplements the shared completion gate below.

| Phase / area | Bounded work and acceptance condition |
|---|---|
| P01 — Baseline | Accepted by Aadil; static inventory, ownership proposals, and limits are in [MIGRATION_BASELINE.md](MIGRATION_BASELINE.md). |
| P02 — Contracts | Accepted by Aadil; [contracts](MIGRATION_CONTRACTS.md) define ownership, financial/API/report rules, dependencies, rollback, fixtures, and the 500 MB ceiling. |
| P03 — Service foundation | Create the .NET solution, configuration, secret handling, and health endpoints; run locally without live database writes. |
| P04 — PostgreSQL foundation | Add connection pooling, explicit transactions, consistent reads, and reviewed migration ownership; prove isolation and rollback on a test database. |
| P05 — Audit and errors | Replace SQLite-dependent audit/error storage with the agreed persistence and attribution; reconcile existing records and protect sensitive inputs. |
| P06 — Coexistence | Establish same-origin route switching and a private report-service client contract; demonstrate routing, authorization, and fallback with stubbed responses. |
| P07 — Authentication | Port login/logout, bcrypt compatibility, expiry, cookie/CSRF behavior, and inactive-user checks; verify session compatibility and rejection cases. |
| P08 — User management | Port users, roles, activation, password reset, and session invalidation; verify administrator controls against the agreed account policy. |
| P09 — Clients | Port client CRUD, selection, pagination, and scope enforcement; verify permitted workflows and cross-client rejection. |
| P10 — Application shell | Host existing assets and navigation through ASP.NET Core; verify login redirects, client context, bookmarks, and cache behavior. |
| P11 — Global settings | Port settings, holidays, date/amount formats, and administrator controls; verify existing pages use the saved values. |
| P12 — Reusable columns | Port master-column CRUD, roles, and dependency checks; verify references survive supported edits and deletions remain guarded. |
| P13 — Templates | Port templates, ordered mappings, purposes, comparison settings, and configuration locks; verify current template behavior. |
| P14 — Saved mappings | Port client/template mapping persistence and reuse; verify ownership, stale configurations, and clear/reset behavior. |
| P15 — Setup backup | Port configuration backup/restore endpoints; verify scope, replacement warnings, validation, and rollback on invalid input. |
| P16 — Upload staging | Port file receipt, owner-bound staging, hashing, sheet/header discovery, and expiry; verify invalid files and missing-source recovery. |
| P17 — Workbook reader | Prove XLS/XLSX cell parity for dates, decimals, formulas, errors, blanks, and source positions using golden workbooks. |
| P18 — Upload preparation | Port header aliases, mapping, date previews, row selection, and permitted overrides; verify stale responses cannot restore obsolete choices. |
| P19 — Input validation | Port critical-field, numeric, date, and source-error checking; verify precise row guidance and zero canonical writes on rejection. |
| P20 — Footing | Port confirmed control rows, exact totals, tolerances, and variance acknowledgement; verify boundary amounts and confirmation binding. |
| P21 — Coverage | Port complete/partial report rules, date confirmation, and account scope; verify missing/empty periods and exclusions follow existing policy. |
| P22 — Duplicate identity | Port canonical identities, exact duplicate checks, multiplicity, and unchanged detection; prove repeat uploads are no-ops. |
| P23 — Corrections | Port correction matching and added/changed/deleted classification; verify ambiguous matches block and legitimate repeated lines survive. |
| P24 — Validation plans | Port signed request/file/configuration checks, expiry, locking, and freshness; reject altered or concurrently superseded plans. |
| P25 — Atomic import | Port current/history writes and action counts in one transaction; prove rollback, simultaneous delivery, and uncertain-commit reconciliation. |
| P26 — Derived fields | Port upload-time derived fields and compact classification/occurrence metadata; verify source preservation and report freshness invalidation. |
| P27 — Import receipts | Port post-import SQL controls, receipts, summaries, and issue workbooks; reconcile stored values independently against source controls. |
| P28 — Upload jobs | Add durable upload job progress, ownership, bounded retries, cancellation rules, and restart recovery; prove interrupted jobs cannot duplicate imports. |
| P29 — Upload acceptance | Exercise the complete existing Upload UI against .NET; compare original/repeat/correction/partial/empty-period cases and measured performance. |
| P30 — Data Bank reads | Port availability, metrics, previews, filters, pagination, and source order; verify totals and stale-cache protections. |
| P31 — History and exports | Port Data Bank history and current/history backup downloads; verify exact rows, changed fields, account scope, and export safety. |
| P32 — Data Integrity | Port inventory and required identity/index safeguards; verify findings against controlled valid and invalid test datasets. |
| P33 — Storage cleanup | Port preview freshness, administrator confirmation, protected-evidence checks, and scoped purge; prove other clients/settings remain intact. |
| P34 — Trial Balance parsing | Port workbook mappings, source validation, totals, and review issues; compare golden Trial Balance inputs and rejected rows. |
| P35 — Trial Balance storage | Port period conflicts, confirmed replacement, hierarchy, uploads, and deletion dependencies; verify atomic replacement and historical scope. |
| P36 — Trial Balance review | Port strict code corrections, classifications, summaries, and assignment reads; verify existing review screens and safeguards. |
| P37 — Statement setup | Port FS/Note categories, outlines, calculations, account mappings, profit linking, and source-bound confirmations; verify configuration and accounting controls. |
| P38 — Cashflow setup | Port Treasury accounts, category assignments, source/column roles, and report settings; verify revision changes and account-specific scope. |
| P39 — Verification setup | Port workstream assignments and account-set draft/preview/publication; verify version and account-code boundaries. |
| P40 — Reconciliation setup | Port account groups, monthly balance inputs, report category sets, and assignments; verify scoped replacement, missing evidence, and independent classifications. |
| P41 — Rule documents | Port Audit Rule Documents, fields, authorization roles, Forms definitions, and specimens; verify references and soft-delete behavior. |
| P42 — Cross-check authoring | Port matrix rows, document/field cells, ordering, and validation; compare saved definitions with the Python baseline. |
| P43 — Rule categories | Port Categories, Assertions, Voucher Checks, memberships, and flags; verify dependencies and inactive-configuration handling. |
| P44 — Rulebook compilation | Port shared/client snapshots, fingerprints, revisions, invalidation, and publication; verify equivalent compiled definitions and concurrency protection. |
| P45 — Historical rules | Port active-session mutation guards, version mismatch, and rebase rules; prove saved results never silently recalculate. |
| P46 — Verification sessions | Port workstream discovery, preview, session creation, presence, and close; verify Treasury boundaries, dates, and user/client scope. |
| P47 — Voucher navigation | Port voucher/source reads, previous/next navigation, range reconciliation, and progress; verify ordering, complete evidence, and recovery. |
| P48 — Evidence linking | Port supporting-data search, assignment, unassignment, and sibling-line resolution; verify cross-session evidence and protected references. |
| P49 — Forms and bills | Port Form entry, Physical Bills, and copy-source validation; verify typed values, ownership, blanks, and atomic saves. |
| P50 — Procedures | Port procedure checks and observation synchronization; verify per-line completion, voucher rollups, and freshness after edits. |
| P51 — Verification engine | Port status gates, cross-check evaluation, and atomic Save/Verify; compare baseline decisions, historical hashes, and concurrent edits. |
| P52 — Audit Workbook | Port Workbook reads, filters, badges, print data, and operational exports; verify shared-modal behavior and Verification parity. |
| P53 — Report data provider | Execute scoped report queries in .NET and supply consistent versioned inputs; verify totals, configuration, completeness, and transfer size. |
| P54 — Python isolation | Extract report helpers from router/database dependencies and accept explicit inputs; prove unchanged calculations on golden datasets. |
| P55 — Report integration | Connect .NET report requests to Python Cashflows, statements, Notes, reconciliations, previews, drilldowns, and exports; verify all outputs. |
| P56 — Report jobs | Coordinate Python generation through .NET job/progress/download APIs; verify source changes, ownership, cancellation, expiry, and worker restarts. |
| P57 — Dashboard/admin | Port dashboard statistics and error-management UI APIs; verify values against authoritative records and administrator permissions. |
| P58 — Google Drive | Port OAuth reconnect, encrypted credential handling, and supported Drive operations; verify authorization, retry, and checksum behavior. |
| P59 — Database backups | Port backup coordination and verified recovery tooling; rehearse isolated restoration and document external-service/file limitations. |
| P60 — Operational tools | Replace required startup, tray, maintenance, and backfill tools; inventory retained test tools and obtain approval for obsolete-script retirement. |
| P61 — Integrated acceptance | Run full workflow, accounting, security, concurrency, network-failure, and performance comparisons; resolve failures without weakening controls. |
| P62 — Deployment rehearsal | Verify containers, regions, costs, monitoring, storage, secrets, backups, and rollback in staging; record an operator runbook. |
| P63 — Controlled cutover | Switch accepted modules in approved stages, observe results, and rehearse rollback; retire legacy backend code only after explicit acceptance. |

## Phase completion and tracking

- Start each phase by confirming exact files and approach under the root workflow; a table row is a planning candidate, not permission to edit.
- Establish baseline failures and known issues before comparisons. Use isolated PostgreSQL, synthetic clients, and controlled workbook copies; preserve originals and existing user changes.
- Verify API/UI compatibility, accounting totals, client isolation, no-write rejection, concurrency, recovery, and report parity where relevant. Acceptance evidence must identify environmental limits.
- Finish with `Change Required` / `Developed As`, pending items including `This phase`, and 4–5 practical test points; identify backend-only checks Aadil can review without repeating.
- Wait for Aadil's local acceptance and explicit authorization before staging, committing, or pushing; use the approved commit message.
- Keep this plan current by rewriting phase status in place. Put detailed evidence/history in the approved project history location; record code-health findings in `KNOWN_ISSUES.md` only with confirmed scope.

## Working rules

- Follow the repository-root `AGENTS.md` and `CLAUDE.md` workflow. For each phase, confirm the candidate file list, then approve the approach before edits; work on one phase at a time.
- Preserve existing user changes and working behavior. Do not expand a confirmed file list without approval.
- Explain decisions in plain language. Obtain approval before adding packages, changing production dependencies, or incurring hosting/storage costs.
- Verify each approved phase in proportion to its risk, summarize changes and remaining checks, and wait for local acceptance and explicit Git approval before staging, committing, or pushing.

## Reference links

- [.NET support policy](https://learn.microsoft.com/en-us/dotnet/core/releases-and-support)
- [ASP.NET Core API choices](https://learn.microsoft.com/en-us/aspnet/core/fundamentals/apis?view=aspnetcore-10.0)
- [Npgsql basic usage](https://www.npgsql.org/doc/basic-usage.html)
- [ExcelDataReader](https://github.com/ExcelDataReader/ExcelDataReader)
- [ASP.NET Core hosted services](https://learn.microsoft.com/en-us/aspnet/core/fundamentals/host/hosted-services?view=aspnetcore-10.0)
- [Render regions](https://render.com/docs/regions) and [private Supabase Storage buckets](https://supabase.com/docs/guides/storage/buckets/fundamentals)
