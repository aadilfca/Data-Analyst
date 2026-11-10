# P01 Migration Baseline

Recorded and accepted by Aadil on 2026-11-10, with instruction to prepare P02. Acceptance covers this static inventory; unresolved contracts and live-data changes require their own decisions.

## Snapshot and coverage

- Branch: main; HEAD: 4992f0e81a742511e5ff84e81f03294b99a2b0a9.
- [migration-baseline.json](migration-baseline.json) inventories 195 Git-visible files with frozen-input hashes, routes, tables, tests, workflows, and proposed ownership.
- Python: 111 files, including 52 test modules and 1,661 static test functions. Parametrized cases are not expanded; tests were not run.
- HTTP: 322 router declarations plus 3 in main.py; 325 declarations and 324 unique method/path pairs.
- Schema: 95 static CREATE TABLE names in database.py, including legacy declarations; live schema, records, indexes, and active use remain unverified.
- Excluded: ignored/private files, environment secrets, workbooks, databases, dumps, caches, dependencies, generated outputs, and binary/business data. No client data or secret values were copied.

## Route inventory

| Source | HTTP declarations |
|---|---:|
| main.py | 3 |
| routers/account_control_center.py | 33 |
| routers/audit_rules.py | 61 |
| routers/auth.py | 4 |
| routers/clients.py | 8 |
| routers/dashboard.py | 8 |
| routers/data.py | 17 |
| routers/data_integrity.py | 1 |
| routers/database_backup.py | 2 |
| routers/error_log.py | 5 |
| routers/google_drive_auth.py | 3 |
| routers/import_export.py | 17 |
| routers/reconciliation.py | 37 |
| routers/settings.py | 11 |
| routers/storage_cleanup.py | 5 |
| routers/templates.py | 22 |
| routers/trial_balance.py | 18 |
| routers/users.py | 6 |
| routers/verification.py | 64 |

main.py:81 and routers/dashboard.py:12 both declare GET /. Source ordering suggests main.py wins; P02 must verify runtime precedence and preserve the intended entry flow.

## Proposed ownership

| Destination | Source groups and treatment |
|---|---|
| .NET | Application entry, identity, configuration, uploads, Trial Balance, account assignments, Audit Rules, Verification, Data Bank, administration, database access, and public report APIs. |
| Python reporting | Financial calculations/workbooks in ledger_group_summary.py, financial_statement_balances.py, and reconciliation_xlsx.py; remove application-layer dependencies during extraction. |
| Split in P02 | routers/reconciliation.py, ledger_report_source.py, reconciliation_balance_source.py, and reconciliation_export_jobs.py mix queries, calculations, configuration, or job coordination. Function candidates do not certify runtime reachability. |
| Browser/vendor assets | Retain HTML/CSS/JavaScript and vendor assets; move hosting/API providers to ASP.NET Core. Preserve response shapes, navigation, loading, and cache behavior. |
| Development/operations | Retain test/experiment evidence; port relevant coverage per .NET phase. Classify supported launchers/backfills/recovery tools in P60. |
| Retirement review | Retired Client Settings pages, historical one-time tools, and legacy table candidates need dependency/data review and explicit removal approval. |

Other shared report helpers need P02 dependency checks. financial_statement_balances.py imports a Trial Balance router helper. Report configuration/query execution move to .NET; financial calculations/rendering remain Python.

## Workflow coverage

The JSON maps identity, reusable configuration, uploads, canonical/history data, integrity/purge, Trial Balance/assignments, Rulebook, Verification/evidence, Audit Workbook, reporting, dashboard/errors, Drive/backups, and local operations to planned phases.

Acceptance must preserve client isolation, exact accounting values, repeated lines, no-op reuploads, correction/deletion history, Footing/coverage gates, atomic writes, frozen Rulebook results, and report freshness. Preserve legacy URLs; review-only prototypes must not become live features.

## Preserved pending work

- routers/import_export.py already has 31 deleted lines removing storage-budget enforcement; its hash includes the patch. Settle intended policy before approving parity expectations.
- File_map.md already has the migration entry and blank-line compression; P01 rewrites that entry without adding rows.
- dotnet-uploader/ was untracked at collection time; no files were staged, committed, or pushed.
- Plan/map index hashes are excluded because P01 updates those indexes. This document and JSON are generated outputs, not frozen inputs.

## Limits and P02 decisions

1. Source declarations do not certify PostgreSQL schema; SQLite audit/error records need a preservation decision.
2. Static authorization indicators omit indirect guards; verify effective access in isolation.
3. Agree report amounts/dates/nulls, scoped snapshots/revisions, completeness, and cross-host job/artifact ownership.
4. Existing Python report export jobs use local files/OS locks, not a PostgreSQL-backed distributed queue.
5. Establish executable test results and golden workbook/report outputs separately; history is not a current pass certificate.

No runtime baseline, performance measurement, database inspection, or restoration proof was performed.

## Aadil's review

- Check that every business area you use appears in the workflow/ownership map.
- Confirm Python retains the intended reports while .NET supplies their data.
- Review the pending storage-budget policy decision without changing its existing patch.
- Review retirement candidates and identify operational tools still needed.
- P01 is accepted; carry unresolved ownership and policy decisions into P02. No local-server test is needed for these documentation artifacts.
