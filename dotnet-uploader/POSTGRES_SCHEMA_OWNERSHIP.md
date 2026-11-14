# PostgreSQL ownership and change gate

P04B source review, 2026-11-11. Deployed Supabase catalog, privileges and capacity metering remain unverified; this reference authorizes no shared-schema changes.

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
| Trial Balance storage | Candidate `0018_trial_balance_storage.sql` owns compact operation/ownership metadata and guards native canonical uploads/accounts/adjustments. Isolated execution only; no legacy adoption, amount-type changes or startup DDL. |
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
