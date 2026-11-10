# P02 Migration Contracts

Version 1, accepted by Aadil on 2026-11-10 with instruction to commit/push and proceed. The [accepted P01 inventory](MIGRATION_BASELINE.md) and [source hashes](migration-baseline.json) identify the starting code. This contract defines migration targets; implementation retains phase-specific approval and runtime parity checks.

## 1. Status and scope

- **Existing:** behavior observed in source or required by the project guide; verify it with isolated fixtures before replacing an endpoint.
- **Target:** proposed implementation contract for review; implementation still follows each phase's file/approach gates.
- **Decided:** Aadil selected a 500 MB Free-plan database ceiling on 2026-11-10. This supersedes the earlier 400 MB ceiling for migration planning; see section 4.
- Move the application backend, database execution, configuration, and public report coordination to .NET. Retain financial calculations/transformations/workbooks in Python and existing browser assets in HTML/CSS/JavaScript.
- Preserve P01 as a historical snapshot. Its pending-policy notes are resolved here, without rewriting its captured source or changing the existing Python patch.

## 2. Public API and access

The inventory contains 325 HTTP declarations, 324 unique method/path pairs, and all 18 routers. Every public route moves behind .NET, including routes invoking Python calculations. A phase must capture its actual request/response fixtures before routing traffic; this architecture contract is not a complete DTO schema for every route.

| Contract | Existing evidence and target |
|---|---|
| Route matching | Preserve methods, paths, query/form/body field names, defaults, omitted/null/empty distinctions, pagination, and ordering. Resolve duplicate GET / declarations by testing main.py's login/client-picker entry before dashboard router inclusion. |
| JSON | Preserve each endpoint's property names, value types, and business-result envelope. Framework-default camelCase or generic error conversion must not change the browser contract. |
| Validation | Preserve strict booleans/integers where declared, accepted legacy fields, multipart names, and current HTTP/business error shapes. FastAPI validation responses must not silently become ASP.NET defaults. |
| Failures | Distinguish HTTP failure from HTTP 200 business rejection. Preserve detail strings/objects, row/column errors, conflict/revalidate guidance, download headers, and safe user messages. |
| Reads/writes | Current api_utils.js retries eligible GET failures once; mutations are not retried. Preserve safe-read behavior; reconcile uncertain write outcomes instead of replaying writes blindly. |
| Identity | Initially share session_token, existing users/sessions, bcrypt hashes, the 10-hour expiry, and session-selected client. No forced ID/hash rewrite. |
| Authorization | Enforce active authenticated users, roles, client context, and object ownership on protected operations. Selected client is context, not a new per-user client-membership permission model. |
| Firm administration | Keep administrator-only user/client mutations, setup, backups, and protected settings. Explicit firm-level routes need no selected client; client-scoped operations need validated scope. Verify administrator report-route exceptions individually. |
| Security changes | Secure production cookies, CSRF protection, inactive-user rejection on protected requests, and consistent guards are target hardening. Current login rejects inactive users, but get_current_user does not recheck is_active; coordinate browser changes in P07 rather than claim existing parity. |
| Public surfaces | Login and intended health/public-information pages remain explicitly public. Missing static authorization evidence is not permission to expose an endpoint; review indirect guards before cutover. |

High-risk request fixtures must cover login/current-client; ImportRequest; multipart Trial Balance validation/import; the embedded Verification save-and-verify bundle; and CashflowExportRequest's expected_fingerprint/expected_identity. Sources: [auth](../routers/auth.py), [uploads](../routers/import_export.py), [Trial Balance](../routers/trial_balance.py), [Verification](../routers/verification.py), [reports](../routers/reconciliation.py), [fetch utility](../static/api_utils.js).

## 3. PostgreSQL and financial values

| Contract | Required behavior |
|---|---|
| Canonical records | client_data is the sole current transaction set, containing raw JSONB plus report-required derived fields. client_data_history alone holds prior changed/deleted row snapshots. No durable transaction mirrors. |
| Scope/keys | Preserve existing IDs, client/template/account scope, source identities/fingerprints, creation time, and source positions. Account codes remain text; preserve leading zeros and configured case semantics. |
| Raw values | Preserve row_data value types and source precision. Existing parsing/formatting rules remain the oracle; do not stringify every public/database number or rewrite stored rows for convenience. |
| Exact comparison | Numeric encodings such as 200 and 200.000 compare by value; blank differs from zero. Reject nonfinite/error inputs under existing rules. Preserve separate raw/concurrency fingerprints and comparison fingerprints. |
| Calculation precision | Exact private monetary fields travel as invariant decimal strings. Use lossless parsing/arithmetic where required; prove .NET decimal range/scale suffices before each numeric port. Preserve the existing Python/SQL engine's calculation policy and round presentation only as configured. |
| Existing approximate helpers | database.py declares extract_amount REAL and derived_debit/derived_credit DOUBLE PRECISION. Do not silently change their types, backfill them, or treat an approximate helper as an exact source amount. Precision repair remains separately scoped. |
| Dates/time | Business dates use date-only values and ISO YYYY-MM-DD internally; timestamps are UTC instants. Preserve raw source date interpretation, display formats, and null/empty defaults; never shift a business date through local timezone conversion. |
| Identity serialization | Port _canonical_source_identity/fingerprint rules with byte-exact hash vectors, including Unicode escaping, field order, numeric formatting, and repeated occurrences. Preserve existing identities; a new algorithm needs an explicit version/migration. |
| Upload policy | Unchanged uploads are no-ops; legitimate repeated posting lines survive. Complete coverage may remove missing rows with history; partial/correction extracts retain absent rows and reject ambiguous pairing. Empty-period clearing requires its existing confirmation. |
| Source controls | Preserve mapped critical fields, confirmed control rows, and exact absolute Footing differences: below 0.01 passes; 0.01 through 1.00 needs bound acknowledgement; above 1.00 blocks. Optional Standard fields follow current template policy. |
| Validation plans | Preserve one ready plan per client/template, the current 30-minute expiry, file/request/configuration hashes, server-authoritative comparison settings, and concurrency locks. skip_validation cannot bypass mandatory controls. |
| Atomicity | Apply current rows, history, counts, required derived changes, and completion controls in one transaction. Preserve savepoint isolation where verification is diagnostic. Lost commit acknowledgement requires status reconciliation. |
| Report reads | Use one read-only consistent snapshot for related inputs. A whole read may restart once with all partial results discarded; never borrow a second independent connection inside that snapshot. |
| Revision/caches | Cache identity includes client, dates, accounts, report options, configuration, and source freshness. Preserve hard refresh, bounded retention, stale-response rejection, and complete-scope totals. |
| Deletion | Protected administrator purge uses fresh preview/confirmation and checks Verification/evidence. Preserve other clients, settings, mappings, Trial Balance, and required indexes. Download initiation does not prove the user saved a backup. |
| Schema ownership | Review one additive migration owner per change; neither runtime independently upgrades shared tables. Inventory actual schema before P04 changes; 95 source-declared names do not prove live presence or justify dropping legacy tables. |
| Audit preservation | .NET records the authenticated actor and preserves audit/error history. Plan the remaining SQLite records' export/reconciliation before removing SQLite-dependent production paths. |

Sources: [database schema/helpers](../database.py), [upload comparison/import](../routers/import_export.py), [accounting conventions](../CLAUDE.md). Existing stored shapes and exact response fixtures take precedence over an incidental .NET serialization default.

## 4. Capacity decision C01 — 500 MB

Aadil chose the Free-plan 500 MB ceiling. Supabase currently lists 500 MB database size per Free project; database quota and physical disk/WAL pressure are separate concepts. Its current disk documentation does not describe 500 MB as the automatic read-only trigger. Sources checked 2026-11-10: [billing quotas](https://supabase.com/docs/guides/platform/billing-on-supabase), [database/disk size](https://supabase.com/docs/guides/platform/database-size).

- Target application ceiling: 500,000,000 bytes (decimal MB). Reconcile the application's measurement/display with provider metering before release; label MiB separately.
- Replace the fixed 400 MB rejection policy with a projected-total check against 500 MB. Include additional transaction/history/index/validation/audit growth and measured uncertainty headroom; do not interpret the ceiling as 500 MB of new workbook data.
- Serialize competing capacity reservations/rechecks; do not let simultaneous jobs each spend the same free space. Refuse work whose projected peak reaches/exceeds the ceiling; treat unavailable capacity measurements as unverified, not unlimited.
- Deletes do not imply reclaimed allocation; measure reusable/allocated space and temporary peaks. Do not introduce automatic compaction or a paid plan.
- The existing 300 MB warning is informational, not a rejection limit. Its presentation can be reviewed with capacity UI; no fixed 400 MB stop remains in the target policy.
- Root CLAUDE.md and Python budget helpers still contain 400 MB, and the pending import_export.py patch removes calls. This phase changes none of them; align both active writers with C01 in the approved P04/P24–P29 work before their cutover. P03's local foundation is not blocked by that implementation.

## 5. Python report function boundary

These are target ownership rules, not instructions to move complete files blindly. The P01 JSON lists individual function candidates; phase-specific extraction must check callers, nested methods, and runtime reachability.

| Source/functions | .NET responsibility | Python responsibility |
|---|---|---|
| routers/reconciliation.py configuration handlers | Groups, balance inputs, category sets/assignments, source/column settings, authorization and storage. | Financial validation helpers invoked with explicit inputs where needed. |
| balance_sheet_report, income_statement_report, financial_statement_notes_report | Read selected TB/accounts/assignments/categories/calculations/profit controls in one snapshot; adapt public response. | build_balance_sheet, build_income_statement, build_statement_notes, financial_statement_context_key and financial checks. |
| _build_report_result, _build_cashflow_result, _build_financial_statement_result, _cashflow_reconciliation_snapshot | Split orchestration, input retrieval, request scope, and public cache identity. | Existing summary/reconciliation calculations and report presentation projections. |
| preview_cashflows, cashflow_drilldown, export_cashflows_report and report preview/download handlers | Public request/filter/page/download behavior; approved SQL projections and scoped evidence retrieval. | Financial result construction and XLSX generation; preserve existing SQL aggregation meaning. |
| ledger_report_source.py load_*/select_* and SQL CTE helpers | Execute canonical queries and load configuration/source identities. Legacy prepared/copy loaders require reachability review; never reinstate retired full-row stores. | build_report_source and pure report normalization/projections, including project_cashflow_treasury_view, after their data inputs are explicit. |
| get_cashflow_report_revision, advance_cashflow_report_revision, invalidate_cashflow_summary_read_models, store_*/prepare_* | Revision, persistence, cache/job lifecycle, and any approved disposable storage; split mixed builders before porting. | Pure financial transformations used by those builders, without storage ownership. |
| reconciliation_balance_source.py load_reconciliation_balance_source/load_uploaded_ledger_balances | Load independent scoped evidence in the shared input snapshot. | build_balance_source, build_uploaded_ledger_balances, build_cashflow_differences/reconciliation/voucher_completeness and adjustment calculations. |
| ledger_group_summary.py, financial_statement_balances.py | Supply typed inputs and configuration. | Keep financial engines; extract the needed build_account_code_hierarchy helper from the Trial Balance router dependency or supply an equivalent proven input hierarchy. |
| reconciliation_xlsx.py | Own authorized job/download metadata and private artifact access. | Keep financial workbook layouts, safe cells, formatting, streaming generation, and control presentation. |
| reconciliation_export_jobs.py ReconciliationExportJobs | Replace public job ownership, scheduling/status/access and expiry coordination for multiple hosts. | Worker-side generation and cancellation checkpoints; OS locks/local metadata alone cannot coordinate separate hosts. |

All other application calculation rules, including upload controls and Verification status gates, move to C#. Upload issue sheets, Data Bank backups, Verification print/operational exports, and database backups are operational outputs; they move with their module unless a later approved contract explicitly assigns rendering to Python.

## 6. Private report input/result protocol — proposed v1

The browser calls .NET. A trusted .NET request supplies a private Python worker with a report kind and immutable input manifest; Python receives neither browser passwords/session cookies nor arbitrary SQL to execute. Final Python reporting imports neither application routers nor database helpers.

| Field group | Contract |
|---|---|
| Identity | protocol_version = report-input/1; opaque request/job ID; report kind; engine version; input digest. Reject unsupported versions instead of guessing. |
| Authorized scope | Client, template/TB upload, period bounds, grouping, Treasury view, and explicit account-selection mode. Omitted legacy All follows existing behavior; an explicit empty/incompatible selection never broadens to All. |
| Source identity | Source upload/record revision, selected TB, configuration/context fingerprints, and UTC capture time. All constituent data comes from the same database snapshot. |
| Input datasets | Required journal/account/category/assignment/calculation/balance/control/evidence sets depend on report kind. Each declares schema version, row count, stable order, completeness, and content hash. Missing differs from a complete empty set. |
| Exact values | Typed money/rates use decimal strings; dates are ISO date-only; instants are UTC; nulls and identifiers retain defined meaning. Raw row_data keeps original JSON types/values. Any public DTO conversion must preserve its existing wire shape. |
| Delivery | Small inputs may be inline; large inputs use streaming/private expiring artifacts. Resolve only server-owned locators, bind access to the job, verify hashes/counts, and reject partial input. Provider/file-size limits require capacity testing. |
| Snapshot lifetime | Materialize a complete bounded input/artifact while holding the read snapshot, then close it before long Python calculation. Never assemble pages from independent changing snapshots. |
| Result | Echo protocol/request/input digest, source identity, options and engine versions; return existing report controls, diagnostics, totals, sections, or an artifact manifest. .NET verifies identity and maps to the existing public API. |
| Artifact | Record private locator, content type, byte length, checksum, owner/scope, source identity, and expiry. Financial files remain temporary; job metadata never becomes another transaction store. |
| Failure | Return a structured private failure/diagnostic tied to the request; .NET emits the endpoint's approved public error. No successful report from incomplete input or substituted scope. |

Source/configuration freshness is part of every result and cache key. Preserve each endpoint's existing current-versus-as-of behavior; never describe mixed snapshots as current. Require an explicit recompute or conflict result when freshness checks fail.

Proposed shared job states are queued, preparing_inputs, running, succeeded, failed, cancelled, and expired; adapt legacy public statuses separately. Persist owner/lease/attempt/progress, use bounded retries over immutable inputs, and handle worker loss. Cancellation after committed mutations cannot imply rollback; financial report workers have no application mutation authority. Final job/storage limits are approved in P28/P56/P62.

## 7. Dependencies and rollback

| Phase band | Entry condition and compatibility bridge |
|---|---|
| P03–P06 | P01 accepted; P02 targets reviewed for the phase. P03 may scaffold locally without live writes. P04 needs catalog evidence, migration ownership, and rollback tests before shared-schema changes. P06 first proves routing with stubs. |
| P07–P15 | Session/client/role behavior must be consistent across both applications before public route switches. Capture current request/response fixtures and verify configuration guards. |
| P16–P29 | Valid client/template/mapping inputs and shared transactions are prerequisites. Settle capacity implementation, hash vectors, coverage/Footing policy, and source revision invalidation before enabling imports. |
| P30–P45 | Preserve existing Verification protection during purge and report consumers during assignment/rule changes. Current Python consumers may remain active through the explicit P06 bridge. |
| P46–P52 | Rulebook/assignment contracts and evidence IDs must already match. Test frozen histories, concurrent edits, and shared-modal behavior before switching writes. |
| P53–P56 | Prove .NET inputs match the old loaders before isolating Python helpers. Keep old report execution available until figures, evidence, controls, and files match. |
| P57–P63 | Reconcile audit/Drive/backup dependencies, full acceptance evidence, deployment/storage limits, and rollback rehearsal before retiring old modules. |

During transition, a remaining Python module may still access its existing database path through the approved bridge; P53–P56 remove report database access for the final design. If a phase requires an unfinished later dependency, split/reorder it or prove the bridge before implementation approval.

Each operation/scope has one active writer. Shadow comparisons use isolated data, not duplicate production imports. Keep schema changes backward-compatible; pause writes and reconcile uncertain commits before routing back. Rollback never deletes history, drops required columns, or restores a whole database over newer unrelated work. Destructive retirement needs separate acceptance, protected backups, and explicit authorization.

## 8. Acceptance fixtures to establish

Expected results come from independent source controls plus the captured existing engine/SQL behavior; unresolved baseline defects are recorded rather than copied or silently fixed. Use synthetic fixtures and approved external workbook copies; never commit client files. These are planned checks, not results already run.

| Set | Required comparisons |
|---|---|
| G01 — API/access | Exact route fields/types/status/error/download shapes; expired/inactive sessions, admin boundaries, selected-client mismatch, and foreign object IDs. |
| G02 — Cells/amounts | XLS/XLSX native/text/serial dates, formulas/errors, Unicode, leading-zero codes, exact rates, trailing-zero equivalence, blank versus zero, nonfinite input, and row order. |
| G03 — Source controls | Footing differences below/at/above 0.01 and 1.00, confirmed control rows, full coverage, optional fields, rejected exclusions, and empty-period confirmation. |
| G04 — Identity/history | Original/repeated/renamed/reordered inputs; explicit IDs, identical repeated lines, unique corrections, ambiguity, full/partial removal, exact history and receipt counts. |
| G05 — Atomicity/capacity | Expired/altered plans, simultaneous imports/reservations, real rollback, lost commit acknowledgement, and projected peaks just below/at/above 500 MB. |
| G06 — Trial Balance/setup | Hierarchy and code corrections, period overwrite, mappings, independent report/Verification classifications, profit context, and missing balance evidence. |
| G07 — Rules/Verification | Rule compilation/versioning, all status gates, evidence/Forms/procedures/observations, save/verify concurrency, session ranges, and historical result protection. |
| G08 — Financial reports | Cashflows/Treasury views, account selections, monthly and broader periods, gross sides/adjustments, Balance Sheet/Income Statement/Notes, full evidence, controls, and workbook outputs. |
| G09 — Jobs/freshness | Source/config changes, mixed/missing inputs, corrupt artifacts, worker restart, cancellation, expiry, concurrent users, and cold/warm cache behavior. |
| G10 — Operations | Current/history exports, guarded purge, restored schema/data/security/sequence checks, credential expiry/reconnect, and legacy URL compatibility. |

P03 establishes the isolated .NET harness; each port phase adds its relevant fixtures, with P61 cross-module acceptance. Existing Python test functions and archived pass counts identify coverage, not a current executable baseline. Performance targets require source-backed timings in comparable environments.

## 9. Acceptance and remaining verification

- C01's 500 MB ceiling is decided; capacity code/helper changes are deferred to their approved phases. No quota, schema, Python source, or live service was changed here.
- Aadil accepted these targets and authorized the documentation checkpoint. P03 is scoped separately; subsequent implementation and Git steps retain their phase-specific approvals.
- P04 verifies actual schema and provider metering. Per-route DTO fixtures, effective authorization, hash vectors, golden accounting outputs, and reachable legacy-code decisions remain explicit work in their listed phases.
- Review the owner split, public API compatibility, monetary/date rules, report manifest, and capacity decision. No local-server test is needed for this documentation phase; later backend checks have their own evidence and manual acceptance points.
