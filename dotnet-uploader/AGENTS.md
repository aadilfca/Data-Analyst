# .NET Application Migration — Codex Plan

## Purpose and status

Migrate the complete application backend, financial calculations, reports, Excel exports and jobs to ASP.NET Core/C#. The folder remains `dotnet-uploader/`; `AuditPlatform.Api` provides the service foundation. Retain existing HTML/CSS/JavaScript. Python remains the working comparison/rollback application until each replacement is accepted; no separate Python report service is planned.

Plan dated 2026-11-10; Aadil approved full C# reporting and reports-before-audit sequencing on 2026-11-11. P01–P07 are locally accepted. See [MIGRATION_BASELINE.md](MIGRATION_BASELINE.md), [migration-baseline.json](migration-baseline.json), and [current contracts](MIGRATION_CONTRACTS.md). Each implementation phase requires file/approach approval.

## Agreed technology direction

- **Runtime/API:** .NET 10 LTS and ASP.NET Core Web API, organized as feature-oriented controllers and service classes in one modular service.
- **Frontend:** retain existing HTML, CSS, JavaScript, navigation, and API behavior while ASP.NET Core takes over hosting and backend requests.
- **Database:** use the existing Supabase PostgreSQL database. Use one configured `NpgsqlDataSource` and parameterized Npgsql/Dapper SQL for explicit transaction control. The C# service must not connect to SQLite.
- **Data contract:** `client_data` remains the only current transaction store; keep raw values and report-required derived fields together. `client_data_history` retains prior changed/deleted row snapshots. Do not add durable full-row transaction copies.
- **Capacity:** Aadil selected the 500 MB Free-plan ceiling; [P02 C01](MIGRATION_CONTRACTS.md) defines projected-total checks. Existing Python/root-guide 400 MB rules await separately approved alignment.
- **Excel:** evaluate ExcelDataReader's row reader for `.xls` and `.xlsx`. Confirm current date, formula-result, Excel-error, source-order, mapping, and Footing behavior against representative workbooks before locking the library.
- **Reports:** use scoped PostgreSQL queries and typed C# calculation services. Approve monetary range/scale and rounding before numeric ports; Python statements currently calculate at 50-digit precision. Evaluate .NET workbook writers against existing large exports before package approval.
- **Schema:** use reviewed, versioned PostgreSQL SQL migrations for approved additive changes. Keep one schema owner for each change; the new service must not run unreviewed schema changes at startup.
- **Long uploads:** use durable PostgreSQL job state with a .NET worker. Persist owner, stage, progress, attempts, and recovery state; make every import step safe to retry. An in-memory queue alone is not durable.
- **Staged workbooks:** if jobs must survive a restart or deploy, use private, short-retention object storage. Never put client workbooks or database backups in Git. Render's service filesystem is ephemeral by default.
- **Authentication:** initially preserve existing `users`/`sessions`, password-hash compatibility, and selected-client behavior. Enforce server-side authorization on every protected operation; use secure, HttpOnly, SameSite cookies and CSRF protection for writes.
- **Hosting and operations:** Docker deployment is suitable for Render. Keep the service near Supabase; Singapore is Render's listed Asian region. Confirm worker availability and monthly cost before production hosting.
- **Verification:** use xUnit and ASP.NET Core `WebApplicationFactory` with a dedicated isolated PostgreSQL database. Compare against golden workbooks and Python report outputs; never rehearse imports against live client data.

## Ownership and report boundary

- .NET owns all backend behavior, including Cashflows, statements, Notes, reconciliations, report transformations, financial workbooks and job/download coordination.
- Vendor/customer, expense and sales quantitative reports also belong in C# when separately specified; this migration does not authorize new reports. Future AI/statistical features need their own evidence and integration decision.
- Report requests stay within .NET: authorized query/snapshot services supply typed inputs to calculation and export services. Preserve existing SQL selection/aggregation semantics, accounting controls and public responses; no Python HTTP client or report protocol is required.
- Capture related inputs in one consistent snapshot; close it before long calculations or exports. Bound memory and temporary private artifacts; preserve precision, dates, nulls, ordering and repeated lines. No durable transaction mirrors.
- Replace Python's local-file/OS-lock export coordination with durable .NET job ownership, bounded concurrency, recovery and expiry. Keep legacy exports active until their replacements pass acceptance.
- Give each production operation/scope one active writer during coexistence. Use isolated databases for comparison imports; route a module only after acceptance and preserve rollback compatibility.

## Phased implementation plan

Execution order: P06 → P07 → P07A–P07C → P08–P40 → P53A–P56 (reports) → P41–P52 (audit modules) → P57–P63. Existing IDs remain stable; P02 records dependencies. P07A sets foundational direction; detailed policies follow the relevant approved phases.

| Phase / area | Bounded work and acceptance condition |
|---|---|
| P01 — Baseline | Accepted by Aadil; static inventory, ownership proposals, and limits are in [MIGRATION_BASELINE.md](MIGRATION_BASELINE.md). |
| P02 — Contracts | Accepted by Aadil; [contracts](MIGRATION_CONTRACTS.md) define ownership, financial/API/report rules, dependencies, rollback, fixtures, and the 500 MB ceiling. |
| P03 — Service foundation | Accepted by Aadil: .NET solution, validated settings, structured console logs, and health endpoints; Release build and nine smoke/configuration checks pass. |
| P04A — PostgreSQL connections | Accepted by Aadil: disabled-by-default settings, bounded singleton pool and readiness; Release build and 32 isolated PostgreSQL tests pass. |
| P04B — Transactions and schema | Accepted by Aadil: atomic writes, read-only snapshots and [schema ownership](POSTGRES_SCHEMA_OWNERSHIP.md); 54 isolated tests pass. Deployed catalog/metering remain prerequisites for shared-schema changes. |
| P05A — Audit/error contract | Accepted by Aadil: [record, attribution, privacy and failure contract](AUDIT_ERROR_CONTRACT.md). Existing source reviewed; deployed records remain unverified. |
| P05B — Audit/error persistence | Accepted by Aadil: required audit writes, bounded diagnostic logging and safe middleware; 90 isolated tests pass. Candidate SQL remains gated from shared-schema execution. |
| P05C — Historical logs | Accepted by Aadil: guarded synthetic reconciliation, source/target checkpoints, ID/time mappings and recovery; 112 local tests pass. Actual data transfer/handoff retain separate approval. |
| P06 — Coexistence | Accepted by Aadil: [routing contract](COEXISTENCE_CONTRACT.md), explicit ownership, default access block and bounded local forwarding; 175 serial tests pass (63 new). Public switching waits for P07; uncertain writes are never replayed. |
| P07 — Authentication | Locally accepted behind disabled settings. See CHANGELOG_ARCHIVE.md, .NET authentication P07. |
| P07A — Collaboration contract | Foundational direction accepted; detailed sharing, verification and presentation policies follow relevant phases. See MIGRATION_CONTRACTS.md. |
| P07B — Tenant foundation | Candidate schema and membership lookup locally accepted; shared-schema execution and module access remain separate gates. |
| P07C — Engagement/sharing foundation | Candidate firm engagements and team eligibility locally accepted. Sharing, action permissions and shared-schema execution remain separately gated. |
| P08 — User management | Tenant-scoped backend and disabled API locally accepted. Shared schema, tenant selection and cutover remain gated. |
| P09 — Clients | Disabled home-workspace CRUD, pagination and guarded selection locally accepted. Sharing, tenant selection and cutover remain gated. |
| P10 — Application shell | Disabled packaged-asset hosting, tenant picker and session context locally accepted. Sharing and live cutover remain gated. |
| P11 — Global settings | Disabled tenant settings/holidays and guarded existing UI locally accepted. Mapping, consumer enforcement and activation remain gated. |
| P12 — Reusable columns | Disabled tenant catalogue and permanent ID/key versus editable-label foundation locally accepted. Legacy bindings and consumer activation remain gated. |
| P13 — Templates | Disabled tenant templates, fixed-key mappings, guarded editor/ledger preparation and Python cache correction locally accepted. Live activation remains gated. |
| P14 — Saved mappings | Disabled tenant/client header mappings, retained revisions, stale-binding review and guarded Upload controls locally accepted. Live activation remains gated. |
| P15 — Setup backup | Disabled tenant configuration export, preview and guarded restore locally accepted. See CHANGELOG_ARCHIVE.md, Configuration backup P15. |
| P16 — Upload staging | Disabled owned local staging/discovery, receipt recovery and expiry locally accepted. See CHANGELOG_ARCHIVE.md, Upload staging P16. |
| P17 — Workbook reader | Typed bounded XLS/XLSX reader and synthetic Python/browser comparisons locally accepted. Source/coercion limits: CHANGELOG_ARCHIVE.md, Workbook reader P17. |
| P18 — Upload preparation | Disabled source-bound mapping, date previews and paged row choices locally accepted. UI integration and validation remain gated. See CHANGELOG_ARCHIVE.md, Upload preparation P18. |
| P19 — Input validation | Disabled source-bound cell/critical-field checks and bounded diagnostics locally accepted. No import authorization. See CHANGELOG_ARCHIVE.md, Input validation P19. |
| P20 — Footing | Disabled exact source/subset totals, confirmed controls and bound variance acknowledgement locally accepted. Precision review/import gates remain. See CHANGELOG_ARCHIVE.md, Footing P20. |
| P21 — Coverage | Disabled declared-period, empty-report and ledger-account checks locally accepted. Source/reference freshness retained; import remains gated. See CHANGELOG_ARCHIVE.md, Coverage P21. |
| P22 — Duplicate identity | Disabled identity review, occurrence matching and scoped no-op evidence locally accepted. Unverified Line/Rate bindings and compatibility cases remain gated. See CHANGELOG_ARCHIVE.md, Identity P22. |
| P23 — Corrections | Disabled correction classification and whole-voucher guards locally accepted. Missing immutable fallback bindings remain gated. See CHANGELOG_ARCHIVE.md, Corrections P23. |
| P24 — Validation plans | Disabled signed temporary plans and guarded freshness/retry checks locally accepted. Import remains disabled. See CHANGELOG_ARCHIVE.md, Validation plans P24. |
| P25 — Atomic import | Disabled transactional current/history writes, protected references and compact recovery locally accepted. Live cutover remains gated. See CHANGELOG_ARCHIVE.md, Atomic import P25. |
| P26 — Derived fields | Disabled permanent bindings, guarded Rates, canonical fields and atomic voucher/freshness metadata locally accepted. See CHANGELOG_ARCHIVE.md, Derived fields P26. |
| P27 — Import receipts | Disabled independent SQL controls, immutable receipts and bounded OOXML evidence locally accepted. See CHANGELOG_ARCHIVE.md, Import receipts P27. |
| P28 — Upload jobs | Add durable upload job progress, ownership, bounded retries, cancellation rules, and restart recovery; prove interrupted jobs cannot duplicate imports. |
| P29 — Upload acceptance | Complete native binding/backup UI integration; exercise Upload against .NET with original/repeat/correction/partial/empty-period cases and measured performance. |
| P30 — Data Bank reads | Port availability, metrics, previews, filters, pagination, and source order; verify totals and stale-cache protections. |
| P31 — History and exports | Port Data Bank history and current/history backup downloads; verify exact rows, changed fields, account scope, and export safety. |
| P32 — Data Integrity | Port inventory and required identity/index safeguards; verify findings against controlled valid and invalid test datasets. |
| P33 — Storage cleanup | Port preview freshness, administrator confirmation, protected-evidence checks, and scoped purge; prove other clients/settings remain intact. |
| P34 — Trial Balance parsing | Port workbook mappings, source validation, totals, and review issues; compare golden Trial Balance inputs and rejected rows. |
| P35 — Trial Balance storage | Port period conflicts, confirmed replacement, hierarchy, uploads, and deletion dependencies; verify atomic replacement and historical scope. |
| P36 — Trial Balance review | Port strict code corrections, classifications, summaries, and assignment reads; verify existing review screens and safeguards. |
| P37 — Statement setup | Port FS/Note configuration, mappings and profit links; bring forward the minimum statement/context calculation needed for source-bound profit confirmation. Verify it against the legacy engine before switching that operation. |
| P38 — Cashflow setup | Port Treasury accounts, assignments, roles and settings; preserve classification/preparation, revision and invalidation effects for both active consumers before switching writes. |
| P39 — Verification setup | Port workstream assignments and account-set draft/preview/publication; preserve vendor-account identification used by Cashflows. This metadata precedes reports; Verification execution follows them. |
| P40 — Reconciliation setup | Port account groups, monthly balance inputs, report category sets, and assignments; verify scoped replacement, missing evidence, and independent classifications. |
| P53A — Report inputs/precision | Port scoped canonical queries, typed inputs, source/configuration identity and snapshot completeness; prove amount/rate range, scale, rounding and lossless overflow handling against source controls. |
| P53B — Financial statements | Port Balance Sheet/Income Statement engines, hierarchy, comparatives, signs, subtotals, profit and transfer controls; reuse P37's proven prerequisite and compare golden results. |
| P53C — Statement Notes | Port nested Notes, category roles, unallocated balances and statement reconciliation; verify current/comparative totals and presentation. |
| P54A — Cashflows | Port periods, Treasury/account selections, classifications, signed reversals, transfers and net/gross presentation; compare amounts, line multiplicity and allocation controls. |
| P54B — Reconciliation/evidence | Port independent balances, posting differences, adjustments, drilldowns and voucher completeness; preserve missing evidence and selected-account boundaries. |
| P55A — Report APIs/screens | Connect C# engines to existing public report APIs/screens; verify response shapes, scope, freshness, cache invalidation and interactive performance. Heavy exports retain legacy ownership until P56. |
| P55B — Financial workbooks | Port financial XLSX layouts, exact values, literal-text safety, complete evidence and bounded writing; compare cells/control totals and measure memory on large workbooks before activation. |
| P56 — Report jobs/acceptance | Run C# generation through durable owner-scoped jobs/progress/download APIs; prove concurrency, cancellation, expiry, restart recovery and end-to-end screen/workbook parity before switching exports. |
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
- Verify API/UI compatibility, accounting totals, tenant/engagement isolation and authorized sharing, no-write rejection, concurrency, recovery and report parity where relevant. Acceptance evidence identifies environmental limits.
- Finish with `Change Required` / `Developed As`, pending items including `This phase`, and 4–5 practical test points; identify backend-only checks Aadil can review without repeating.
- Wait for Aadil's local acceptance and explicit authorization before staging, committing, or pushing; use the approved commit message.
- Keep this plan current by rewriting phase status in place. Put detailed evidence/history in the approved project history location; record code-health findings in `KNOWN_ISSUES.md` only with confirmed scope.

## Working rules

- Follow the repository-root `AGENTS.md` and `CLAUDE.md` workflow. For each phase, confirm the candidate file list, then approve the approach before edits; work on one phase at a time.
- Preserve existing user changes and working behavior. Do not expand a confirmed file list without approval.
- Explain decisions in plain language. Obtain approval before adding packages, changing production dependencies, or incurring hosting/storage costs.
- Verify each approved phase in proportion to its risk, summarize changes and remaining checks, and wait for local acceptance and explicit Git approval before staging, committing, or pushing.

## Local foundation commands

Run PowerShell from this folder. SDK 10.0.404/runtime 10.0.15 live in ignored `.dotnet/`; `.tools/` holds portable PostgreSQL 17.11, package caches and evidence. Exact package versions are pinned in the project files.

```powershell
$env:DOTNET_CLI_HOME = "$PWD\.tools\cli-home"
$env:DOTNET_CLI_TELEMETRY_OPTOUT = '1'
$env:DOTNET_GENERATE_ASPNET_CERTIFICATE = 'false'
$env:DOTNET_ADD_GLOBAL_TOOLS_TO_PATH = 'false'
$env:TESTINGPLATFORM_TELEMETRY_OPTOUT = '1'
$env:NUGET_PACKAGES = "$PWD\.tools\packages"
& .\.dotnet\dotnet.exe restore .\AuditPlatform.slnx --source https://api.nuget.org/v3/index.json --packages $env:NUGET_PACKAGES
& .\.dotnet\dotnet.exe build .\AuditPlatform.slnx --configuration Release --no-restore
& .\.dotnet\dotnet.exe run --project .\tests\AuditPlatform.Api.Tests --configuration Release --no-build --no-restore -- -noColor -maxThreads 1 -result-trx "$PWD\.tools\p06-results\p06.trx"
& .\.dotnet\dotnet.exe run --project .\src\AuditPlatform.Api --configuration Release --no-build --no-restore
```

- Launch settings bind `http://127.0.0.1:5080`; stop an existing foundation instance before starting another. Ctrl+C stops a foreground run.
- `/healthz` returns 200 independently of connectivity. `/health/ready` returns 503 when disabled/unavailable/timed out, or 200 after bounded `SELECT 1`; readiness proves connectivity only.
- PostgreSQL defaults off. Explicit `Postgres__Enabled=true` and secret `Postgres__ConnectionString` enable it; host/database/username are required. Python settings and `.env` are not loaded. Remote connections require `SSL Mode=VerifyFull`.
- `Postgres__MaximumPoolSize` defaults to 10 (range 1–100); connection/command/readiness timeouts default to 5/30/3 seconds (ranges 1–30/1–300/1–15). These settings override connection-string pool/time limits; pooling and state reset stay on, sensitive driver detail/parameter logs stay off.
- Tests use [EDB's portable PostgreSQL 17.11 archive](https://www.enterprisedb.com/download-postgresql-binaries), extracted to `.tools/postgresql-17.11/pgsql/`; `AUDITPLATFORM_TEST_POSTGRES_BIN` may select another approved binary folder inside `.tools/`. Each run creates a random SCRAM-protected loopback cluster/database and stops it; fixture logs/data remain ignored. No existing connection URL is accepted.
- Run xUnit through the executable command above; the .NET 10 default `dotnet test` VSTest mode rejects this package's MTP integration. Serial execution avoids observed concurrent fixture-startup timeouts. Windows sandbox restriction may require normal local execution for `initdb`; no Windows service is installed.
- Transaction/snapshot callers must follow the [runtime service contract](POSTGRES_SCHEMA_OWNERSHIP.md#runtime-service-contract). Tests create synthetic tables and use a local relay to prove lost-commit acknowledgement without production fault hooks.
- `PostgresAuditWriter` requires the supplied write context and trusted server inputs; module ports integrate it with their mutations. Diagnostic capture uses safe symbols and route templates. `LogPersistence__...` limits are defined in [the audit/error contract](AUDIT_ERROR_CONTRACT.md); diagnostics never read request bodies, queries or cookies.
- Candidate `database/migrations/0001_audit_error.sql` is executed only by owned test fixtures here. Runtime startup performs no DDL. Deployed catalog/capacity evidence and explicit execution approval remain required; P05C handles historical reconciliation and P07/P57 authentication/dashboard integration.
- `tools/AuditPlatform.LogRehearsal` uses the installed ASP.NET shared runtime and existing packages, but starts no web host. Tests generate owned PostgreSQL clusters and synthetic JSON; the CLI requires `--input`/`--owner-dir` plus secret environment variable `AUDIT_REHEARSAL_CONNECTION`. Input stays in that owned run directory; it does not read SQLite or Python settings. See [reconciliation gates](AUDIT_ERROR_CONTRACT.md#historical-reconciliation-and-delivery-gates).
- `Coexistence` ships disabled with an empty route table. P07 authentication is disabled by default; the [bridge contract](COEXISTENCE_CONTRACT.md) defines access, exact registrations, loopback limits and rollback.
- P07 requires `Authentication__Enabled=true` and a canonical `Authentication__PublicOrigin`; HTTP is limited to localhost. Shared audit schema verification and separate approval precede deployment.
- P07B's candidate `database/migrations/0003_tenant_foundation.sql` runs only in owned isolated fixtures; startup never applies it. It seeds no records, and repeat application fails safely. Membership lookup grants no client or engagement access.
- P07C's candidate `database/migrations/0004_engagement_foundation.sql` is isolated-fixture only; startup never applies it. Team eligibility grants no client access or action permission.
- P08's `database/migrations/0005_tenant_user_roles.sql` is fixture-only and grants no admin automatically. `UserManagement__Enabled` defaults false; tenant mapping, multi-tenant selection and Python parity precede any activation.
- P09's `ClientManagement__Enabled` defaults false; it changes no schema. See MIGRATION_CONTRACTS.md, P09 isolated tenant clients, for record/selection permissions and activation gates.
- P10's `ApplicationShell__Enabled` defaults false and requires authentication/user/client flags. Candidate `0006_session_tenant_context.sql` is fixture-only; packaged frontend hosting grants no later-module API or sharing access. P10 browser tests use existing `venv`/Chromium.
- P11's `SettingsManagement__Enabled` defaults false and requires the shell. Candidate `0007_tenant_settings.sql` is fixture-only; tenant policy writes cannot activate before upload/report consumers enforce the same scope. See MIGRATION_CONTRACTS.md, P11 isolated tenant settings.
- P12's `ColumnManagement__Enabled` defaults false and requires the shell. Candidate `0008_tenant_columns.sql` adds tenant definitions and identity guards without seeding/copying/rekeying legacy data. See MIGRATION_CONTRACTS.md, P12 column identity and labels, before porting any column consumer.
- P13's `TemplateManagement__Enabled` defaults false and requires shell/columns. Candidate `0009_tenant_templates.sql` is fixture-only; see MIGRATION_CONTRACTS.md, P13 tenant templates and bindings, before enabling consumers.
- P14's `SavedMappings__Enabled` defaults false and requires native templates. Candidate `0010_tenant_saved_mappings.sql` is fixture-only; action grants and binding/revision rules are in MIGRATION_CONTRACTS.md, P14 saved headers.
- `Platform__ApplicationName` and `Platform__DatabaseCapacityLimitBytes` override non-secret defaults. Invalid settings exit with code 1 before listening; the 500,000,000-byte setting is configuration only until capacity enforcement is ported.
- Supply future secrets through approved environment/secret-store configuration, never appsettings.json, launch settings, or logs. Private settings files, SDKs, caches, and build outputs are ignored.
- Windows 10 Pro 22H2 is outside the current .NET 10 supported-OS list. Aadil approved local development with this limitation; a local pass does not certify supported production hosting. P62 validates deployment separately.
- Evidence: ignored `.tools/p03-verification.json`, `.tools/p04a-verification.json`, `.tools/p04b-verification.json`, `.tools/p05b-verification.json`, `.tools/p05c-verification.json`, `.tools/p06-verification.json` and `.tools/p06-results/p06.trx`. Local checks do not certify deployed Supabase/TLS/catalog/metering or migrated workflows.

## Reference links

- [.NET support policy](https://learn.microsoft.com/en-us/dotnet/core/releases-and-support)
- [ASP.NET Core API choices](https://learn.microsoft.com/en-us/aspnet/core/fundamentals/apis?view=aspnetcore-10.0)
- [Npgsql basic usage](https://www.npgsql.org/doc/basic-usage.html)
- [ExcelDataReader](https://github.com/ExcelDataReader/ExcelDataReader)
- [ASP.NET Core hosted services](https://learn.microsoft.com/en-us/aspnet/core/fundamentals/host/hosted-services?view=aspnetcore-10.0)
- [Render regions](https://render.com/docs/regions) and [private Supabase Storage buckets](https://supabase.com/docs/guides/storage/buckets/fundamentals)
