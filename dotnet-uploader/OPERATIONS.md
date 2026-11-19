# C# local operations

## Start the C# development server

Build `AuditPlatform.slnx` in Release, then run `start_csharp_tray.cmd`. This opens the tray; choose **Start C# server** from its menu to start the .NET API at `http://127.0.0.1:5080`. Wait for its running status, then choose **Open C# app** or visit that URL. The tray checks `/health/app-ready` and confirms the root page returns HTML; it stops only the server process it started. Exiting the tray leaves the server running. It never searches for or stops the Python server on port 5000.

The tray expects `AuditPlatform.Api.dll` beside itself in a deployment package. The repository command sets that path to the Release build output, prefers the repository's local .NET host, and defaults to the .NET `Development` environment. The API's `UserSecretsId` lets ASP.NET load per-user development settings without putting them in Git. User Secrets are not encrypted, so use only a dedicated local/test database credential; never use a production credential or commit the values. For production, use the host's managed secret settings. The tray does not read Python `.env` files or transfer their credentials.

The safe application defaults keep PostgreSQL and the C# shell off. The readiness gate checks database connectivity, shell/authentication/user/client configuration, then confirms the UI is served. Until configured, the tray shows a specific not-ready reason instead of treating a blank/404 host as running. ApplicationShell requires Authentication, UserManagement and ClientManagement enabled together. User and client administration writes have separate `WriteEnabled` switches, default off; login, tenant selection, and client viewing do not require them. Configure a dedicated non-production database with the reviewed schema first, then set these local values with the repository's .NET host from the project root:

```powershell
$dotnet = '.\dotnet-uploader\.dotnet\dotnet.exe'
$project = '.\dotnet-uploader\src\AuditPlatform.Api\AuditPlatform.Api.csproj'
& $dotnet user-secrets set 'Postgres:Enabled' 'true' --project $project
& $dotnet user-secrets set 'Postgres:ConnectionString' '<dedicated local/test PostgreSQL connection string>' --project $project
& $dotnet user-secrets set 'ApplicationShell:Enabled' 'true' --project $project
& $dotnet user-secrets set 'Authentication:Enabled' 'true' --project $project
& $dotnet user-secrets set 'Authentication:PublicOrigin' 'http://127.0.0.1:5080' --project $project
& $dotnet user-secrets set 'UserManagement:Enabled' 'true' --project $project
& $dotnet user-secrets set 'UserManagement:WriteEnabled' 'false' --project $project
& $dotnet user-secrets set 'ClientManagement:Enabled' 'true' --project $project
& $dotnet user-secrets set 'ClientManagement:WriteEnabled' 'false' --project $project
```

These commands do not create or migrate a database. Apply only reviewed migrations to the isolated test database, and use a test account that exists in that database. The login `codex/codex` is usable only if it was created there. Keep both write switches false while Python shares the database; they disable user creation/edit/deactivation/password reset and client create/edit/deactivation. The runtime role also has no user/client administration database writes. After configuration, rebuild Release, close/reopen the tray, choose **Start C# server**, wait for **C# server: running**, then open the app. See Microsoft's [development secret guidance](https://learn.microsoft.com/en-us/aspnet/core/security/app-secrets?view=aspnetcore-10.0).

Column and Template pages are also disabled by default. After their reviewed schema and least-privilege runtime `SELECT` grants are present, set `ColumnManagement:Enabled` and `TemplateManagement:Enabled` to `true` to allow reads; leave each `WriteEnabled` false until the editing workflow is separately accepted. Neither setting applies SQL or changes existing records.

`start_tray.vbs`, `tray_control.py`, and `run.bat` remain the Python rollback launchers until P63. Do not replace their target during P60.

## P62 Render candidate

The independent workspace-adoption rehearsal is described below; it does not activate this deployment candidate.

`Dockerfile` and `Dockerfile.dockerignore` build a Linux API image using only C# source and the shared frontend assets; repository data, Python files, secrets, tests and build output are excluded. `render-csharp-p62.yaml` is a separate, inactive Blueprint candidate. The root `render.yaml` remains the live Python service. No Blueprint was synced and no service was created.

The candidate uses Render's Singapore region and Free web plan only for short-lived, non-production rehearsal. Free services can sleep and have limited resources; this is not a production plan. Confirm that Singapore is near the separate test database before creating the service because Render does not let an existing service change region. Current Render pricing lists the smallest always-on paid web plan at $7/month (0.5 CPU, 512 MB RAM); that is a price baseline, not a capacity recommendation, and excludes database, bandwidth, storage and workspace costs. Production sizing and spend still need approval. See [Render regions](https://render.com/docs/regions), [compute plans](https://render.com/docs/compute-plans), and [pricing](https://render.com/pricing).

Before creating this candidate, provide a dedicated non-production PostgreSQL database with reviewed schema and synthetic or approved sanitized records. Set `Postgres__ConnectionString` and the canonical HTTPS `Authentication__PublicOrigin` in Render's secret/configuration UI; remote PostgreSQL requires `SSL Mode=VerifyFull`. The Blueprint does not apply SQL. Review and apply the required migrations to this isolated database only; `0017`, `0022`, and `0023` remain candidates. Enable Dashboard/error administration or other modules only after their schema prerequisites are verified. Because a Render web service is public, use synthetic records and a strong one-off test password; do not seed or reuse `codex/codex` there. Never supply the live database URL or the shared Python `.env` values.

No persistent disk is configured because local database backup and upload-job features stay off in this Linux preview. Render's filesystem is ephemeral; any file-backed feature requires a deliberate private storage plan before enablement. Local full-database backup remains a Windows operator workflow; cloud database backup/restore evidence must come from the database provider. The service uses console JSON logs and `/health/app-ready`; Render health checks route traffic only after a successful database, application-configuration and shell response. The candidate enables the shell's required user/client settings against the isolated test database only. No external monitoring or alert channel is configured yet. See [Render health checks](https://render.com/docs/health-checks), [persistent disks](https://render.com/docs/disks), and [Docker builds](https://render.com/docs/docker).

The Linux image built locally on 2026-11-19. A loopback container used the configured development Supabase connection and its TLS certificate mounted read-only. `/healthz`, `/health/app-ready`, and the login page returned 200; `/health/ready` had one startup timeout, then passed five consecutive checks. The root redirected to login and an unauthenticated current-client request returned 401. User/client writes, backups, and upload jobs stayed off. The temporary container was removed; the image remains local and the tray server on port 5080 was untouched.

P62 still needs a separate Render preview service and Aadil's manual feature review. Use `render-csharp-p62.yaml` only with the non-production database; never sync the root Python Blueprint or use production credentials/data. Keep Python available for the P63 rollback rehearsal.

P61 is limited to system readiness: database connection, reviewed schema/migration state, tray startup/shutdown ownership, logging, backup/recovery and rollback checks. Automated isolated database/readiness and backup/recovery checks pass; the interactive tray menu and user-configured local DB remain operator checks. Feature-level Verification and upload/preview acceptance is deferred to Aadil's manual test in the P62 staging deployment; keep unaccepted modules disabled and do not treat a successful health check as feature acceptance. P62 is a separate non-production deployment rehearsal. P63 production cutover is a separate authorization gate and still requires manual feature acceptance, a protected recovery copy on another device, and the provider production baseline. Keep the Python service available as rollback until P63 is accepted.

## Owned local workspace adoption

`tools/AuditPlatform.WorkspaceAdoption` provides separate workspace, template/header, upload-role and Rulebook-definition stages. It preserves existing records and grants no engagement assignments. Historical Verification evidence, Account Ledger import and report ownership remain separate work.

Supply a connection only through `AUDIT_ADOPTION_CONNECTION`. The tool accepts only `127.0.0.1`, a `backup_rehearsal_*` database and an `--owner-dir` whose marker and actual PostgreSQL data directory match the generated recovery cluster. It cannot target production. `--workspace-name` supplies the rehearsal label; `--unassigned-users` lists explicitly excluded account IDs from a protected inventory. Unknown active roles block mapping unless excluded; inactive accounts remain inactive. Never commit this private input.

Without `--apply`, it performs a read-only preview and prints counts/fingerprint. Applying requires that same `--expected` fingerprint and a unique `--operation` GUID. Mapping and a compact audit receipt commit atomically. After a lost acknowledgement, repeat the same operation/fingerprint to reconcile its receipt; do not create a new operation automatically. An existing workspace without that receipt is rejected. Only supported non-secret setting keys are copied; unsupported legacy keys remain untouched for review. Keep detailed results under protected recovery storage.

## Supabase development workspace adoption

Use `--target supabase-development` for the one allowlisted test Supabase project; the default owned-local mode remains unchanged and cannot target remote databases. Supply its operator URI only through `AUDIT_ADOPTION_SUPABASE_CONNECTION` in a protected one-time process environment and its CA file through `AUDIT_ADOPTION_SUPABASE_ROOT_CERT`. The tool enforces the exact project identity, port 5432, PostgreSQL operator role and `SSL Mode=VerifyFull`; it never reads `.env`, prints credentials or saves the operator secret. The app itself uses the separate restricted C# runtime login.

The remote preview is read-only and outputs aggregate user/role counts, a fingerprint and any required migration; it does not show account/client IDs or setting values. The current approved source has one active roleless account, which remains unassigned; a different count or unsupported active role blocks the preview. Migration `0007_tenant_settings.sql` and the approved initial workspace adoption are complete on this test project. Fresh targets require the migration and an independently reviewed preview before any apply; supply `--apply`, the exact `--expected` fingerprint and a fresh `--operation` GUID. Apply is atomic and receipt-bound. No production target is supported.

After workspace adoption, `--stage templates --tenant-id <rehearsed ID>` previews permanent column/template keys and saved Excel headers against current/history keys using shared native validation. It requires an empty native definition catalogue; existing IDs, literal keys/headers, policies and inactive states remain. The existing Account Ledger notes field stays separate metadata.

For the reviewed backup, include `--retain-unresolved` in preview/apply: convert 131 headers and retain eight original references for review (six inactive-client entries, two obsolete column bindings). Candidate `0024_saved_mapping_adoption_review.sql` is required locally. Native reads flag affected scopes; only a successful explicit scoped Save/Clear resolves them. Inactive clients remain inaccessible until separately reactivated. All 139 legacy entries stay intact; source comparison excludes only additive metadata and the two new local operation receipts. Production execution remains gated.

After template adoption, `--stage upload-bindings --tenant-id <ID> --binding-plan <private filename>` requires an explicit version-1 plan directly inside the owned directory. Its `tenantId` and `templates` selections cover every active ordinary template; each selection gives `templateId`, `fields` (`role`, `columnId`, literal `storageKey`) and optional `review`. Unknown roles, wrong keys/types, omitted/duplicate templates and existing bindings reject the plan. The preview fingerprint binds the exact plan bytes and current native configurations; apply/reconciliation use the existing operation protocol. Runtime consumers keep their locks; this preview uses a read-only snapshot.

The reviewed plan binds five templates/12 roles. Vendor Bill's automatic Rate lacks the legacy `Quantity` field; Aadil chose `review: rate_quantity_missing`, leaving its bindings and calculation unchanged. Do not substitute Receive Quantity or change Rate mode. Ledger is excluded: its purpose still blocks native import pending dedicated balance/identity/history acceptance. No raw/derived financial values, saved headers or existing validation plans are rewritten; cutover must drain old requests separately. Role storage uses candidate `0014`; this step adds no SQL migration.

After configuration adoption, `--stage audit-rules --tenant-id <ID> --actor-username <existing administrator> --retain-unresolved` previews the complete legacy rule graph. Candidate `0025_audit_rule_adoption.sql` records kind-qualified old/new IDs, Form-field/specimen maps and unresolved specimen references. The actor must be an existing active workspace administrator; it identifies who performed the new copy, without inventing historical authorship. Existing rule creation timestamps and specimen timestamps are retained. The same preview fingerprint/operation protocol applies; an existing native Rulebook catalogue rejects first adoption.

The reviewed source contains 89 definitions (30 Documents, 16 Cross-checks, 21 Categories, seven Assertions, 15 Voucher Checks), 21 field declarations and 21 specimens. Eighteen current Form fields and 20 specimens convert; three unused declarations on an inactive non-Form Document and one specimen with a missing role remain referenced for review. Inactive states, required flags, authority roles, matrix order/cells and original system flags remain recorded. Shared C# validators/compiler publish a new version and Category snapshots atomically. One existing active Category requirement references an inactive Document; its warning is preserved for acceptance. New native sequences may retain failure gaps and are never rewound.

Definition adoption leaves original rules/snapshots, all sessions/details, saved Forms/values/check states and transaction data untouched. It does not retag evidence, rebase/resume sessions, certify evaluator parity or enable Verification. Use the ID maps in the separate historical-evidence adoption step; review original system-document handling there. Provenance references currently protect copied specimens from hard deletion; resolve that lifecycle during feature acceptance before any Verification activation. Keep runtime flags off until those gates pass.

The reviewed backup has five sessions on legacy Rulebook versions 308 and 53, but only version 311 compiled snapshots remain. Aadil chose to retain all five as read-only history. C# now blocks review, lifecycle writes and rebase for versions 53/308; the authenticated archive view and historical evidence adoption remain prerequisites before Verification activation. Under the revised deployment scope, defer this feature acceptance to manual staging review and keep Verification disabled until accepted.

The backup also has legacy `development_allow_unbacked_uploads` and `voucher_patterns` settings. No active source-code consumer was found, so retain their original rows for rollback and do not copy either into native settings; in particular, never enable the development upload bypass in C#.

Trial Balance uploads/accounts/assignments and report setup already live in the shared PostgreSQL source tables used by C# P35/P36 and P53–P56. No row copy or ID rekey is needed; database ownership and read/write safety remain system checks, while report behavior is deferred to manual staging review.

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
