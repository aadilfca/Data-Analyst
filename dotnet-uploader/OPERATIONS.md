# C# local operations

## Start the C# development server

Build `AuditPlatform.slnx` in Release, then run `start_csharp_tray.cmd`. This opens the tray; choose **Start C# server** from its menu to start the .NET API at `http://127.0.0.1:5080`. Wait for its running status, then choose **Open C# app** or visit that URL. The tray checks `/health/app-ready` and confirms the root page returns HTML; it stops only the server process it started. Exiting the tray leaves the server running. It never searches for or stops the Python server on port 5000.

The tray expects `AuditPlatform.Api.dll` beside itself in a deployment package. The repository command sets that path to the Release build output, prefers the repository's local .NET host, and defaults to the .NET `Development` environment. The API's `UserSecretsId` lets ASP.NET load per-user development settings without putting them in Git. User Secrets are not encrypted. The C# migration has a dedicated Supabase development project (`klljugkujxxmtbmbeewn`); use its separate restricted runtime login, never the database-owner or Python `.env` credential. For Render, use its secret settings. The tray does not read Python `.env` files or transfer their credentials.

The safe application defaults keep PostgreSQL and the C# shell off. `Postgres:Profile` selects `Supabase` (default) or `Local`; the existing `Postgres:ConnectionString` remains the Supabase fallback, while `Postgres:LocalConnectionString` is stored separately. The readiness gate proves database connectivity with `SELECT 1`, not that application tables or data exist. ApplicationShell requires Authentication, UserManagement and ClientManagement enabled together. User and client administration writes have separate `WriteEnabled` switches, default off; login, tenant selection, and client viewing do not require them.

## Supabase empty-schema foundation

The last documented read-only catalog review found the dedicated project's public application schema empty with no migration ledger. Recheck the project before any setup because this is not a live status check. `database/bootstrap/legacy_schema.sql` is a guarded empty base for the legacy tables required by C# setup and reviewed migrations; it creates no roles, grants, users, clients, definitions, uploads or transaction rows. It includes the template settings added by Python's PostgreSQL schema helper and the canonical session/active-identity indexes.

The reviewed order is the bootstrap, then migrations `0001_audit_error.sql` through `0010_tenant_saved_mappings.sql` in numeric order. These files are one-time schema changes, not an automatic migration runner; the project has no migration ledger. Before an approved apply, verify the schema is still empty, review each file and its checksum, and record each successful step outside Git. If any result is interrupted or unclear, stop and inspect the live catalog before retrying. Do not apply `0011` or later upload, history, cleanup or other module migrations as part of core setup; each needs its own schema/permission review.

After migrations 0001–0007, run `database/permissions/runtime_core_grants.sql` once as the database owner. It creates `audit_platform_runtime` with `LOGIN` and `NOINHERIT`, no password, and grants session management, limited login bookkeeping, tenant/client-card reads, and audit/diagnostic inserts. Set its password interactively with `psql` using `\password audit_platform_runtime`; do not put a password in SQL Editor text, a command line, or a file. The narrow updates to `users.last_login` and session-selection fields support the existing login/context row locks; they do not allow password or client edits. The script does not grant user/client administration, Columns/Templates reads or writes, transaction/history access, module writes, schema creation, or future-object default privileges. Add grants for each module only after reviewing its tables and behavior. Never put either database password in this repository; use .NET User Secrets locally and Render secret settings for deployment.

### C# Data Bank read-only activation

For the dedicated development project only, apply `database/permissions/runtime_data_bank_read_grants.sql` as the database owner. It grants the restricted runtime login `SELECT` on five holiday-calendar columns only. Enable local current-data reads with `DataBank:Enabled=true`; the C# app shell, PostgreSQL and native tenant-template reads must also be enabled. Settings Management can remain off. Keep `DataBank:HistoryEnabled=false` and `DataBankExports:Enabled=false` until those features are separately accepted. Missing optional upload-frequency metadata uses the Daily default. Account Ledger preview remains blocked until `uploaded_files.ledger_account_code` is supported.

### Owned local PostgreSQL

The repository includes portable PostgreSQL 17.11 tools. From the repository root, initialize a persistent local cluster once, then use its lifecycle commands:

```powershell
& .\dotnet-uploader\scripts\local-postgres.ps1 Initialize
& .\dotnet-uploader\scripts\local-postgres.ps1 Status
& .\dotnet-uploader\scripts\local-postgres.ps1 Start
& .\dotnet-uploader\scripts\local-postgres.ps1 Stop
```

Initialization creates a loopback-only server on port 55432, a separate non-superuser C# role, and an empty database in ignored `.tools/local-postgres/`. It stores the local connection in .NET User Secrets and an ACL-protected local file; it does not change the selected profile, inspect Python `.env`, or contact Supabase. The database is empty: restore and verify an approved schema/data snapshot before selecting it, or login, users, clients, Columns and Templates will fail because their tables/data are absent.

After that restore is verified, select the local database for the tray-launched app. Switch back to the existing Supabase connection without re-entering its password:

```powershell
& .\dotnet-uploader\scripts\local-postgres.ps1 UseLocal
& .\dotnet-uploader\scripts\local-postgres.ps1 UseSupabase
```

The script never deletes or replaces an existing cluster. Stop it only through its `Stop` action; start it with `Start`. Local and Supabase profile choices are saved separately from their connection strings.

### Supabase C# configuration

After the new project's schema and runtime grants are reviewed, set these local values with the repository's .NET host from the project root. Use the exact endpoint shown in that project's Connect dialog. The direct endpoint requires IPv6; use the shared session pooler on port 5432 when the network is IPv4-only. Keep the Supabase root certificate available as a local file and include its path as `Root Certificate` in the connection string; keep both the connection string and certificate outside Git. The application uses the restricted runtime login, never the setup owner.

```powershell
$dotnet = '.\dotnet-uploader\.dotnet\dotnet.exe'
$project = '.\dotnet-uploader\src\AuditPlatform.Api\AuditPlatform.Api.csproj'
& $dotnet user-secrets set 'Postgres:Enabled' 'true' --project $project
& $dotnet user-secrets set 'Postgres:Profile' 'Supabase' --project $project
& $dotnet user-secrets set 'Postgres:ConnectionString' '<restricted runtime connection string with SSL Mode=VerifyFull and Root Certificate=<local certificate path>>' --project $project
& $dotnet user-secrets set 'ApplicationShell:Enabled' 'true' --project $project
& $dotnet user-secrets set 'Authentication:Enabled' 'true' --project $project
& $dotnet user-secrets set 'Authentication:PublicOrigin' 'http://127.0.0.1:5080' --project $project
& $dotnet user-secrets set 'UserManagement:Enabled' 'true' --project $project
& $dotnet user-secrets set 'UserManagement:WriteEnabled' 'false' --project $project
& $dotnet user-secrets set 'ClientManagement:Enabled' 'true' --project $project
& $dotnet user-secrets set 'ClientManagement:WriteEnabled' 'false' --project $project
```

These commands do not create or migrate a database. Apply only reviewed migrations to the selected database, and use a test account that exists there. The login `codex/codex` is usable only if it was created there. Keep both write switches false while Python shares the database; they disable user creation/edit/deactivation/password reset and client create/edit/deactivation. The runtime role also has no user/client administration database writes. After configuration, rebuild Release, close/reopen the tray, choose **Start C# server**, wait for **C# server: running**, then open the app. See Microsoft's [development secret guidance](https://learn.microsoft.com/en-us/aspnet/core/security/app-secrets?view=aspnetcore-10.0).

Column and Template pages are disabled by default. The new project's migration state and native definition tables have not been checked. Review its schema before applying migrations; preview reusable-definition adoption and separately review least-privilege runtime read grants before enabling `ColumnManagement:Enabled` or `TemplateManagement:Enabled`. Leave each `WriteEnabled` false until the editing workflow is separately accepted. Neither setting applies SQL or changes existing records. Do not copy client transaction rows.

`start_tray.vbs`, `tray_control.py`, and `run.bat` remain the Python rollback launchers until P63. Do not replace their target during P60.

## P62 Render candidate

The independent workspace-adoption rehearsal is described below; it does not activate this deployment candidate.

P62 deployment is blocked until the dedicated C# project's Data API is disabled and `database/permissions/runtime_api_isolation.sql` is separately applied and verified. The script removes current public grants and prevents postgres-created public tables/sequences from granting direct access to anon/authenticated; it cannot change supabase_admin defaults or PostgreSQL's global PUBLIC EXECUTE default for future routines. Keep the Data API disabled. The app uses its server-side PostgreSQL runtime login; browser pages call the C# API. Keep RLS unchanged until runtime-role policies are designed. See POSTGRES_SCHEMA_OWNERSHIP.md.

`Dockerfile` and `Dockerfile.dockerignore` build a Linux API image using only C# source and the shared frontend assets; repository data, Python files, secrets, tests and build output are excluded. `render-csharp-p62.yaml` is a separate, inactive Blueprint candidate. The root `render.yaml` remains the live Python service. No Blueprint was synced and no service was created.

The candidate uses Render's Singapore region and Free web plan only for short-lived, non-production rehearsal. Free services can sleep and have limited resources; this is not a production plan. Confirm that Singapore is near the separate test database before creating the service because Render does not let an existing service change region. Current Render pricing lists the smallest always-on paid web plan at $7/month (0.5 CPU, 512 MB RAM); that is a price baseline, not a capacity recommendation, and excludes database, bandwidth, storage and workspace costs. Production sizing and spend still need approval. See [Render regions](https://render.com/docs/regions), [compute plans](https://render.com/docs/compute-plans), and [pricing](https://render.com/pricing).

Before creating this candidate, use the dedicated C# development project only while it contains development/test data. Render uses IPv4, so configure the exact session-pooler endpoint from Supabase Connect, a restricted C# runtime login, `SSL Mode=VerifyFull`, and the CA certificate as a protected Render secret file. Set `Postgres__ConnectionString` and the canonical HTTPS `Authentication__PublicOrigin` in Render's secret/configuration UI. The Blueprint does not apply SQL. Inspect the new schema and review each required migration before applying it. Enable Dashboard/error administration or other modules only after their schema and grants are verified. Because a Render web service is public, use synthetic records and a strong one-off test password; do not seed or reuse `codex/codex` there. Never supply database-owner credentials or Python `.env` values.

No persistent disk is configured because local database backup and upload-job features stay off in this Linux preview. Render's filesystem is ephemeral; any file-backed feature requires a deliberate private storage plan before enablement. Local full-database backup remains a Windows operator workflow; cloud database backup/restore evidence must come from the database provider. The service uses console JSON logs and `/health/app-ready`; Render health checks route traffic only after a successful database, application-configuration and shell response. The candidate enables the shell's required user/client settings against the isolated test database only. No external monitoring or alert channel is configured yet. See [Render health checks](https://render.com/docs/health-checks), [persistent disks](https://render.com/docs/disks), and [Docker builds](https://render.com/docs/docker).

The Linux image built locally on 2026-11-19. A loopback container used the configured development Supabase connection and its TLS certificate mounted read-only. `/healthz`, `/health/app-ready`, and the login page returned 200; `/health/ready` had one startup timeout, then passed five consecutive checks. The root redirected to login and an unauthenticated current-client request returned 401. User/client writes, backups, and upload jobs stayed off. The temporary container was removed; the image remains local and the tray server on port 5080 was untouched.

P62 still needs a separate Render preview service and Aadil's manual feature review. Use `render-csharp-p62.yaml` only with the designated project while it remains development/test-only; never sync the root Python Blueprint or use database-owner credentials. Keep Python available for the P63 rollback rehearsal.

P61 is limited to system readiness: database connection, reviewed schema/migration state, tray startup/shutdown ownership, logging, backup/recovery and rollback checks. The designated project connection, TLS, login, Users and Client Cards are verified locally; Columns/Templates still need existing-definition adoption and runtime read grants, and tray-menu review remains. Feature-level Verification and upload/preview acceptance is deferred to Aadil's manual test in P62; keep unaccepted modules disabled and do not treat a successful health check as feature acceptance. P62 is a separate non-production deployment rehearsal. P63 production cutover is a separate authorization gate and still requires manual feature acceptance, a protected recovery copy on another device, and the provider production baseline. Keep the Python service available as rollback until P63 is accepted.

## Owned local workspace adoption

`tools/AuditPlatform.WorkspaceAdoption` provides separate workspace, template/header, upload-role and Rulebook-definition stages. It preserves existing records and grants no engagement assignments. Historical Verification evidence, Account Ledger import and report ownership remain separate work.

Supply a connection only through `AUDIT_ADOPTION_CONNECTION`. The tool accepts only `127.0.0.1`, a `backup_rehearsal_*` database and an `--owner-dir` whose marker and actual PostgreSQL data directory match the generated recovery cluster. It cannot target production. `--workspace-name` supplies the rehearsal label; `--unassigned-users` lists explicitly excluded account IDs from a protected inventory. Unknown active roles block mapping unless excluded; inactive accounts remain inactive. Never commit this private input.

Without `--apply`, it performs a read-only preview and prints counts/fingerprint. Applying requires that same `--expected` fingerprint and a unique `--operation` GUID. Mapping and a compact audit receipt commit atomically. After a lost acknowledgement, repeat the same operation/fingerprint to reconcile its receipt; do not create a new operation automatically. An existing workspace without that receipt is rejected. Only supported non-secret setting keys are copied; unsupported legacy keys remain untouched for review. Keep detailed results under protected recovery storage.

## Supabase development workspace adoption

The target is the dedicated C# development project `klljugkujxxmtbmbeewn`. Its public application schema was catalog-reviewed as empty, with no migration tracking rows; default privileges still need a least-privilege plan, and a restricted runtime login must be separately established. This workspace-adoption path expects an existing active administrator and is not the first-run setup tool. Use `--target supabase-development` only after the exact endpoint is allowlisted. Supply the setup-owner URI only through `AUDIT_ADOPTION_SUPABASE_CONNECTION` in a protected one-time process environment and the CA file through `AUDIT_ADOPTION_SUPABASE_ROOT_CERT`. The tool must verify the exact project, setup-owner identity and `SSL Mode=VerifyFull`; never read `.env`, print credentials or save the owner secret. The running app must use a separate restricted runtime login.

Preview is read-only and fingerprint-bound. Review the new project's schema and preview results before approving any migration or adoption. Transfer only reviewed reusable definitions; never copy client transaction rows. Use the exact preview fingerprint and a fresh operation ID for any separately approved apply. Keep all modules off until their schema, grants and behavior are accepted. Previous-project adoption counts and plans do not describe this target.

The template, upload-binding and Rulebook adoption stages need a fresh source/destination review before use. For template adoption, apply reviewed migrations `0010`, `0024`, and `0026` separately first. The tool accepts either an empty definition catalog or an exact legacy catalog match; it rejects partial, changed, mixed, or already-native bindings. If the old saved-header table is absent, it treats that source as empty; migration `0024` is still required before apply. Never promote bindings with manual SQL. Preserve unresolved references for review and keep original definitions and transaction data intact. Historical evidence, Ledger import, reports and feature activation remain separate acceptance gates.

## Cross-project reusable definition transfer

`tools/AuditPlatform.DefinitionTransfer` moves only the global reusable catalog: `master_columns`, `templates`, and `template_column_mappings`. It preserves definition IDs, names, types, categories, notes, template purpose/ledger settings, active/system/subtotal flags, and mapping roles/order/comparison policy. Template creator names are replaced with `Definition Transfer` and created dates use the destination transfer time. It never reads client rows, saved client headers, users, uploads, `client_data` or history.

Before preview, review and apply the destination empty-schema bootstrap/migrations and grants separately. Set `AUDIT_DEFINITION_TRANSFER_SOURCE_CONNECTION` to a SELECT-only source login, `AUDIT_DEFINITION_TRANSFER_TARGET_CONNECTION` to the destination owner login for this controlled setup, and `AUDIT_DEFINITION_TRANSFER_ROOT_CERT` to the supplied Supabase CA file in a protected one-time process environment; never put these values in arguments, `.env`, logs or Git. The command also requires the exact source project reference. Both connections must be Supabase PostgreSQL on port 5432; the destination is restricted to the dedicated C# project. TLS uses `VerifyFull`.

From `dotnet-uploader/`, run `./.dotnet/dotnet.exe run --project ./tools/AuditPlatform.DefinitionTransfer -- --source-project-ref <SOURCE_PROJECT_REF>`. Review the full definition preview, fingerprint and empty destination counts. Apply is a separate controlled action and requires that fingerprint, a fresh operation GUID and the exact phrase `TRANSFER-DEFINITIONS <SOURCE_PROJECT_REF> TO klljugkujxxmtbmbeewn`. Apply is atomic, receipt-bound and rechecks source definitions and destination emptiness. It rejects tenant-scoped source catalogs and any existing destination catalog. Do not apply a preview without separate approval. After any approved transfer, preview the existing same-project Templates adoption stage separately; keep all features off.

## Supabase first-run administrator setup

`tools/AuditPlatform.FirstRunSetup` is a one-time controlled setup tool for the dedicated C# development project. Run it only after the empty schema and prerequisite migrations have been separately reviewed and applied. The shared owner password must be rotated before production use. From `dotnet-uploader/`, use `\.dotnet\dotnet.exe run --project .\tools\AuditPlatform.FirstRunSetup`. The tool prompts for the direct or Session Pooler endpoint, asks for the owner password without echoing it, and requires the Supabase Root 2021 CA file. It enforces `SSL Mode=VerifyFull`, checks the project-specific host/username format and TLS session, and stops unless the required login/workspace/client/audit schema is present and the application tables are empty (apart from the standard unknown-error reference row).

Review the on-screen preview and type the exact project confirmation before it writes one workspace-scoped administrator (the global legacy role stays empty), one `audit_firm` workspace, one admin membership, one blank client card, its workspace link and one system audit event in a single transaction. It does not create tables, roles, grants or migrations, copy reusable definitions or client transactions, or turn on application features. If the commit result is uncertain, inspect the database before any retry. The Python app remains available for rollback.

### Restricted runtime login

For runtime role creation, password setup, grants, and secret storage, follow the steps under **Supabase empty-schema foundation** above. Use the restricted login for the C# app; keep feature flags off until permissions and behavior are accepted. The owner login is only for controlled setup.

## Maintenance and one-time scripts

| Existing tool | P60 disposition |
|---|---|
| C# Setup full-database backup and isolated restore | P59 native replacement; default off, local only. |
| C# Data Bank current/history workbook before selected-template purge | Existing P33 behavior; required proof gate stays in front of purge. |
| `storage_cleanup.py`, `audit_postgres_indexes.py` | Python global maintenance remains available for the rollback app; no C# global maintenance or index-removal command is exposed. |
| `backfill_rate.py`, `backfill_extract_amount.py`, and `scripts/backfill_*.py` | Historical/repair scripts, not C# startup tasks. Keep offline and review each against current schema/data before any rerun. |
| `scripts/rehearse_database_backup.py` | Legacy Python recovery rehearsal; retain as rollback evidence. C# isolated restore has its own fixture test. |
| Python test and experiment files | Retain as historical evidence; not production launch or maintenance dependencies. |

No Python operational file is removed or invoked by the C# tray. Script retirement needs a separate inventory-backed approval after P63 acceptance.
