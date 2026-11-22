# PostgreSQL ownership and change gate

P04B source review, 2026-11-11; initial deployed public-schema assessment, 2026-11-18. Catalog capture does not authorize schema changes or certify migration compatibility.

## Cutover assessment — initial evidence

Existing configured PostgreSQL inspected using the repository venv driver directly, without importing Python application/bootstrap code. A TLS-encrypted client connection used one READ ONLY REPEATABLE READ transaction, bounded queries and rollback. No business rows, passwords, sessions or source payloads were read. Private catalog evidence is ignored under `.tools/cutover-assessment-20261118/`; no credentials or catalog dump enter Git.

- PostgreSQL 17.6: 96 public tables, 870 columns, 334 constraints, 281 indexes, 84 sequences; no public user triggers or policies observed. These are catalog facts, not correctness findings.
- Zero `platform_*` tables. Thirty tables declared across the candidate SQL are missing. All four table names declared by `0001` exist, but presence does not prove compatible definitions or an applied migration. No public table name resembling a migration ledger was found; this does not exclude external migration tracking.
- Session/template mappings lack native ownership columns. All 12 explicitly added columns in `0022` and the tenant column in `0023` are absent. Tenant/user/client association and permanent template/key adoption must be explicitly rehearsed; column presence alone cannot establish complete migration state.
- `pg_database_size` reports 461,106,323 bytes (461.1 decimal MB). This is not Supabase quota-meter evidence. Confirm provider metering and peak space for additive tables/indexes/backfills; perform no deletion or compaction based on this measurement.
- The inspected role can create databases/roles and bypass RLS; plan a separately provisioned C# runtime role with reviewed minimum grants. Object-level grants, TLS certificate verification and provider dashboard settings still need acceptance.

The 2026-11-22 C# project review found public API grants for `anon` and `authenticated` across 48 application tables, 30 sequences, and 15 trigger routines. `runtime_api_isolation.sql` removes current access and future `postgres` defaults without changing the restricted runtime grants. `postgres` cannot change `supabase_admin` defaults, so P62 requires the Supabase Data API to stay disabled. RLS remains off until safe runtime-role policies are designed.

## Schema reconciliation — execution prerequisites

Offline comparison of the captured catalog against all 23 candidates records source checksums privately; this is not an applied-migration ledger. All 44 explicit legacy-column type guards in `0001` match. Native audit metadata is absent apart from existing `error_log.client_id`; retain old rows/IDs and rehearse additive metadata/index creation. The candidates' explicit table prerequisites resolve in numeric order over the existing schema, but dynamic SQL, full definitions, record-dependent checks and runtime access still require restored-database execution.

Python's already-bootstrapped path calls 28 schema helpers; fresh bootstrap also declares the shared tables and seed data. Static local-call analysis finds 94 declared table names, of which only `google_drive_credentials` is absent from the captured catalog. Do not recreate that retired integration. Imported/dynamic helpers and data-dependent branches are not certified by this analysis. C# must adopt the reviewed existing schema baseline; starting Python bootstrap is not a migration procedure.

Migration `0022` defects are repaired in the working copy: resolve procedure uniqueness structurally, and adapt all nine Python upserts to the legacy partial-index predicate after cached schema detection. Restart Python after a schema change. Regression proves legacy saves, native tenant separation and untouched native observations. This does not prove Python reads of mixed native/legacy evidence or full rollback compatibility; those remain gated. Before 2026-11-19, no candidate SQL was executed remotely. On 2026-11-19, `0001`, `0003`–`0007` were applied to the test Supabase project; the approved adoption added eight memberships, 35 client homes and six settings. Source user/client/settings rows remain unchanged; production was not accessed.

Isolated recovery passed on 2026-11-18: a fresh read-only exported snapshot supplied the logical archive and controls; all public application records, schema/permission controls, sequence states and canonical accounting totals match the restored database. Protected evidence is external to Git; see CHANGELOG_ARCHIVE.md, C# isolated snapshot recovery. This does not certify Supabase-managed services, offline disaster recovery or C# application acceptance.

All 25 candidates replay successfully on a fresh verified local restore; original values across all 96 tables match after excluding additive metadata and using identical UTC/numeric serialization. Rehearsal checksums are private execution evidence, not a production migration ledger. See CHANGELOG_ARCHIVE.md, C# Rulebook-definition adoption.

## Workspace foundation adoption

The owned-local C# `WorkspaceAdoption` tool is preview-first, fingerprint-bound and transactional, with operation receipts for retry reconciliation. Rehearsal preserved all 10 accounts/passwords, mapped eight recognised-role memberships (inactive stays inactive), associated 35 clients and copied six supported settings. Aadil chose to leave the active unset-role account unassigned; an inactive unset-role account also receives no membership. No engagement grants are inferred. Original values across all 96 tables matched; one new local audit receipt is explicitly excluded from that comparison. Production remains untouched; see CHANGELOG_ARCHIVE.md, C# workspace foundation adoption.

The test Supabase project has the core schema through `0007`, a separate restricted C# login and one approved workspace adoption. Eight memberships, 35 client-home links and six supported settings were added; the active roleless account remains unassigned, two accounts have no membership, and no engagement assignments were inferred. The local shell uses that login; client-profile reads are column-scoped, while user/client administration writes remain disabled in both API settings and database privileges. Settings grants remain pending; production was not accessed.

The separate template rehearsal adopts 78 columns, 10 templates (seven active), 93 fixed-key bindings and 131 saved headers. Aadil chose to retain eight original references for review: six inactive-client entries and two obsolete column bindings. Candidate `0024` tracks these; scoped successful Save/Clear resolves native review flags. All 139 legacy headers remain. Current/history keys match; Account Ledger's existing reserved notes metadata stays separate. See CHANGELOG_ARCHIVE.md, C# template and saved-mapping adoption.

The upload-role stage uses an explicit protected plan, permanent IDs/keys and shared native validation; it binds five ordinary templates/12 roles without changing stored financial values or saved headers. Aadil chose to retain Vendor Bill's missing automatic-Rate Quantity binding for review; Receive Quantity is not substituted. Account Ledger remains separate because native import still rejects ledger purpose. See CHANGELOG_ARCHIVE.md, C# upload-role adoption.

The separate Rulebook-definition stage uses candidate `0025` ID/review maps and shared native validation/compilation. Definitions and valid specimens copy without modifying original rules or Verification work; stale declarations/specimen references remain for review. Historical evidence, system-document handling and session rebase are not adopted by this stage. See CHANGELOG_ARCHIVE.md, C# Rulebook-definition adoption.

Next: complete local tray/login/read-only acceptance and review tenant-settings grants. Historical evidence and Trial Balance/report ownership, native Ledger import, Vendor Bill's retained gate, `development_allow_unbacked_uploads`, and `voucher_patterns` remain open. Confirm provider quota/peak allocation; review historical logs and mixed-backend rollback. Keep Python operational until P63 acceptance.

## Inventory and ownership

The frozen [P01 inventory](migration-baseline.json) records 95 source-declared table names. Of these, 93 have explicit PostgreSQL declaration sites; `storage_cleanup_runs` uses shared SQL and `upload_lifecycle_runs` a shared helper, both called by PostgreSQL bootstrap/upgrade paths in `database.py`. Declarations do not prove deployed presence, current usage or retirement eligibility. Keep P01 unchanged.

| Area | Ownership during migration |
|---|---|
| Existing PostgreSQL objects | Python's `init_db_postgres` and approved `_ensure_postgres_*`/shared upgrades remain the schema owners until a specific change is transferred. Review fresh-bootstrap and already-bootstrapped paths together. |
| New .NET operations | P04A/P04B provide runtime connections and transactions only. Transfer database execution by accepted module phases under [P02](MIGRATION_CONTRACTS.md); runtime startup performs no DDL. |
| Upload staging | Candidate `0011_upload_staging.sql` owns native receipt metadata linked to new `uploaded_files` rows; isolated execution only, no legacy adoption or startup DDL. |
| Validation plans | Candidate `0012_upload_validation_plans.sql` adds native retry envelopes and shared one-ready uniqueness; Python retains plan-table declarations. Isolated execution only; conflicts require review. |
| Atomic imports | Candidate `0013_upload_imports.sql` adds nullable history metadata and compact native outcomes; no canonical table rebuild or startup DDL. Isolated execution only. |
| Upload derivations | Candidate `0014_upload_derived_fields.sql` adds native binding metadata and a nullable canonical freshness marker; existing raw values/derived types remain unchanged. Isolated execution only. |
| Import receipts | Candidate `0015_upload_import_receipts.sql` owns immutable compact completion evidence and scoped row references, never raw-row mirrors; imported receipts share the outcome transaction. Isolated execution only. |
| Upload jobs | Candidate `0016_upload_jobs.sql` owns immutable scoped requests, leases/attempts and compact completion references; success shares the import/receipt transaction. Private object storage holds temporary workbooks. Isolated execution only. |
| Scoped cleanup | Candidate `0017_storage_cleanup.sql` owns compact outcomes/artifact recovery and scoped purge exceptions to staging/plan retention guards. Isolated execution only; existing migration files remain unchanged. |
| P57 diagnostic ownership | Candidate `0023_error_log_tenant_scope.sql` adds nullable tenant ownership and scoped indexes; no historical inference/backfill or startup DDL. Isolated execution only. |
| Saved-mapping adoption review | Candidate `0024_saved_mapping_adoption_review.sql` stores compact unresolved legacy references; originals remain untouched. Owned-local adoption only; runtime startup performs no DDL. |
| Rulebook adoption identities | Candidate `0025_audit_rule_adoption.sql` owns compact rule/Form/specimen ID maps and unresolved references. Source definitions, frozen legacy snapshots and evidence remain unchanged; local operator only. |
| Trial Balance storage | Candidate `0018_trial_balance_storage.sql` owns compact operation/ownership metadata and guards native canonical uploads/accounts/adjustments. Isolated execution only; no legacy adoption, amount-type changes or startup DDL. |
| Trial Balance review | Candidate `0019_trial_balance_review.sql` owns immutable review intent/outcomes and narrow native account-edit permissions. Source fields, amounts, adjustments and import ownership stay immutable; isolated execution only. |
| Audit/error persistence | Existing SQLite-dependent paths need P05's explicit PostgreSQL design; do not infer replacement tables from the source inventory. |
| Legacy/prepared/temporary objects | Source presence grants no right to recreate or remove them; retain P01's reachability/retirement review and P02's canonical-store contract. |

## Before a shared-schema change

1. Obtain scoped read-only deployed evidence: provider/version/role, schemas, columns/types/defaults/nullability, collations, constraints/indexes, sequences, triggers, policies and grants; compare with both Python startup paths. Keep credentials/client data outside Git.
2. Verify actual allocation and provider metering against [P02 C01](MIGRATION_CONTRACTS.md); source counts and a compact local fixture cannot establish production headroom.
3. Name one owner and reviewed version/checksum for the SQL change. Coordinate any Python startup upgrade touching the same objects; neither application may independently repeat a conflicting migration.
4. Rehearse the exact SQL on an isolated representative schema, including already-applied behavior, data/ID preservation, existing Python/report compatibility, capacity peaks and recovery. Record nontransactional operations separately.
5. Approve files, SQL, backup/recovery evidence and execution separately. Prefer additive compatibility; destructive retirement remains an explicit later acceptance. Deploying the web service must not apply schema changes automatically.

## Runtime service contract

- `PostgresTransactions.ExecuteAsync` owns one explicit READ COMMITTED transaction. Pass its context to awaited nested work; callbacks must not end/dispose supplied resources, start another root operation, or borrow a second connection for related work.
- Writes run once. Callback/cancellation failures roll back; commit transport/cancellation uncertainty raises `PostgresCommitOutcomeUnknownException`. A definite PostgreSQL commit rejection retains its server error. Reconcile durable operation/receipt state before another write attempt; this foundation does not implement module-specific reconciliation.
- `PostgresReadSnapshot.ExecuteAsync` uses REPEATABLE READ, READ ONLY and ends by rollback. Return complete materialized data before calculation/rendering; no live readers/iterators, unawaited tasks, external publication or other side effects inside its retryable callback.
- A recognized disconnect permits one complete read retry on a new lease/snapshot. Partial results are discarded; cancellation, SQL errors and statement/lock timeouts are not retried. A replacement snapshot may reflect newer committed data.
- These are trusted application callbacks, not a sandbox for arbitrary SQL. SQL waits use the existing command timeout as transaction-local `statement_timeout`, with `lock_timeout` capped at five seconds; cleanup has a separate two-second cancellation budget. These limits do not bound arbitrary callback CPU/external work. Keep long processing outside database scopes.

Local verification and commands are referenced from [AGENTS.md](AGENTS.md); deployed evidence remains a prerequisite before the first shared-schema change.
