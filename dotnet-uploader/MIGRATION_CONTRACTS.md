# P02 Migration Contracts

Version 3: P07A–P07C precede P08 to address tenant and audit-firm collaboration; detailed business rules remain open. The [P01 inventory](MIGRATION_BASELINE.md) and [source hashes](migration-baseline.json) remain historical evidence. Phase-specific approvals and runtime parity checks still apply.

## 1. Status and scope

- **Existing:** behavior observed in source or required by the project guide; verify it with isolated fixtures before replacing an endpoint.
- **Target:** proposed implementation contract for review; implementation still follows each phase's file/approach gates.
- **Decided:** Aadil selected a 500 MB Free-plan database ceiling on 2026-11-10. This supersedes the earlier 400 MB ceiling for migration planning; see section 4.
- Move all backend behavior, financial calculations, reports, workbooks and jobs to .NET; retain existing HTML/CSS/JavaScript. Python remains the legacy comparison/rollback application until replacements are accepted; no separate Python report service is planned.
- Preserve P01 as a historical snapshot. This contract supersedes its ownership proposals and pending-policy notes without rewriting captured source or changing the existing Python patch. P01–P05C implementation remains reusable.

## 2. Public API and access

The inventory contains 325 HTTP declarations, 324 unique method/path pairs, and all 18 routers. Every public route and report calculation moves to .NET. Capture actual request/response fixtures before routing traffic; this architecture contract is not a complete DTO schema for every route.

| Contract | Existing evidence and target |
|---|---|
| Route matching | Preserve methods, paths, query/form/body field names, defaults, omitted/null/empty distinctions, pagination, and ordering. Resolve duplicate GET / declarations by testing main.py's login/client-picker entry before dashboard router inclusion. |
| JSON | Preserve each endpoint's property names, value types, and business-result envelope. Framework-default camelCase or generic error conversion must not change the browser contract. |
| Validation | Preserve strict booleans/integers where declared, accepted legacy fields, multipart names, and current HTTP/business error shapes. FastAPI validation responses must not silently become ASP.NET defaults. |
| Failures | Distinguish HTTP failure from HTTP 200 business rejection. Preserve detail strings/objects, row/column errors, conflict/revalidate guidance, download headers, and safe user messages. |
| Reads/writes | Current api_utils.js retries eligible GET failures once; mutations are not retried. Preserve safe-read behavior; reconcile uncertain write outcomes instead of replaying writes blindly. |
| Identity | Initially share session_token, existing users/sessions, bcrypt hashes, the 10-hour expiry, and session-selected client. No forced ID/hash rewrite. |
| Authorization | Enforce active user, tenant membership, role, engagement assignment, approved sharing and object scope as applicable. Client selection is context, never proof of permission. |
| Firm administration | Keep administrator-only user/client mutations, setup, backups, and protected settings. Explicit firm-level routes need no selected client; client-scoped operations need validated scope. Verify administrator report-route exceptions individually. |
| Security changes | Secure production cookies, CSRF protection, inactive-user rejection on protected requests, and consistent guards are target hardening. Current login rejects inactive users, but get_current_user does not recheck is_active; coordinate browser changes in P07 rather than claim existing parity. |
| Public surfaces | Login and intended health/public-information pages remain explicitly public. Missing static authorization evidence is not permission to expose an endpoint; review indirect guards before cutover. |

High-risk request fixtures must cover login/current-client; ImportRequest; multipart Trial Balance validation/import; the embedded Verification save-and-verify bundle; and CashflowExportRequest's expected_fingerprint/expected_identity. Sources: [auth](../routers/auth.py), [uploads](../routers/import_export.py), [Trial Balance](../routers/trial_balance.py), [Verification](../routers/verification.py), [reports](../routers/reconciliation.py), [fetch utility](../static/api_utils.js).

### Tenant and collaboration direction

- A business or CA firm can be a tenant. Business users can include directors/CEO; firm users can include administrators, managers and internees. A firm can serve multiple businesses and engagements.
- An authorized business–firm relationship permits controlled collaboration on a defined engagement. Assigned audit staff and permitted business users see only the scope approved for them; membership or client selection alone grants no engagement access.
- P07A maps the real workflow and decides ownership, existing-data mapping, consent/revocation, assignments, CEO visibility and when data is called verified. No automatic publication or sharing rule is assumed.
- P07B/P07C follow separately approved scopes. Preserve canonical transaction/history contracts, legacy compatibility and one active writer; rehearse migrations/rollback in isolated databases before any shared-schema execution.
- Every migrated operation, report, cache, job, export and download must enforce and carry the applicable authorized tenant/engagement context; prevent stale access after revocation. Reconcile safeguards across Python/.NET before switching routes.

### P07A workflow and confirmed decisions

- Current app: users select a client; uploads pass mapping/validation and controls; auditors record procedures, evidence and observations; Audit Workbook shows voucher-level Cleared, Query or Pending. These statuses do not by themselves define CEO access or final audit approval.
- Current firm practice: the audit team uploads ERP Excel files. Future ERP-database integration should support automatic data fetching; its scope, access, safeguards and implementation require a separate brief and approval.
- Intended engagement outcome: the manager reaches final agreement with management; the audit team records management's stance/response against each observation. CEO financial summaries start with the existing Balance Sheet, Income Statement and Cashflows reports, with a show/hide column for observations and responses. Report detail, timing, visibility, agreement process and authorization remain to be specified.
- Agreement alone does not resolve an uncorrected issue: reports continue to show it as outstanding. The assigned audit team verifies corrections with manager review before resolution. Historical report/evidence access after sharing ends is determined by the specific arrangement; preserve audit history and apply the approved access policy.
- Platform administrator Aadil configures control of access/sharing and CEO visibility/timing separately for each business and engagement. The CA firm's administrator or an authorized manager assigns/removes engagement managers and internees; platform and tenant administration are distinct.
- Business-record ownership is determined by the specific arrangement. For CEO financial summaries, verified means audit checks were completed and reviewed by a manager, limited to the specific checked data and period; outstanding observations remain visible. If a later upload changes verified data, mark the affected data as needing audit review again. Detailed evidence and change-scope rules remain to be agreed.
- Policy configuration, delegation and defaults require explicit decisions. Configuration alone must not silently grant access outside its approved tenant, engagement, role and object scope; record who changed access and preserve audit history.
- Open: how arrangements record/enforce ownership and map existing data; verification evidence/change scope and how observations connect to CEO reports; who may invite/revoke and how arrangement-based historical access is enforced; CEO-visible timing and policy controls; assignment limits and exceptions; sharing across firms and revocation effects.
- P07A remains in progress until these rules and workflow are agreed. P07B/P07C implementation, schema changes, deployed access and activation remain separately approved.

## 3. PostgreSQL and financial values

| Contract | Required behavior |
|---|---|
| Canonical records | client_data is the sole current transaction set, containing raw JSONB plus report-required derived fields. client_data_history alone holds prior changed/deleted row snapshots. No durable transaction mirrors. |
| Scope/keys | Preserve existing IDs, client/template/account scope, source identities/fingerprints, creation time, and source positions. Account codes remain text; preserve leading zeros and configured case semantics. |
| Raw values | Preserve row_data value types and source precision. Existing parsing/formatting rules remain the oracle; do not stringify every public/database number or rewrite stored rows for convenience. |
| Exact comparison | Numeric encodings such as 200 and 200.000 compare by value; blank differs from zero. Reject nonfinite/error inputs under existing rules. Preserve separate raw/concurrency fingerprints and comparison fingerprints. |
| Calculation precision | Use typed exact amounts internally and lossless serialization where persisted/transferred. Python statements use 50-digit Decimal precision; C# decimal has 28–29 significant digits. Prove supported ranges, scale, intermediates, overflow and rounding before each numeric port. If insufficient, approve a lossless alternative; never silently narrow accepted inputs or substitute double. Preserve calculation policy and configured presentation rounding. |
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

## 5. C# report ownership

All report layers move to C#. Check callers and runtime reachability before porting mixed files; preserve proven SQL and accounting semantics. P01's Python-retention proposals are superseded, while its captured code remains the comparison baseline.

| Existing source | C# responsibility / acceptance |
|---|---|
| routers/reconciliation.py | Configuration, authorized queries, report APIs, previews/drilldowns, diagnostics and download coordination; preserve wire shapes and source-bound confirmations. |
| ledger_report_source.py | Canonical SQL, scoped source selection, normalization, Treasury projections, revisions and invalidation; never reinstate retired durable transaction copies. |
| financial_statement_balances.py | Statements, Notes, account hierarchy, current/comparative balances, signs, subtotals, profit linking and transfer controls; remove router-helper coupling in the C# design. |
| ledger_group_summary.py | Cashflows and group summaries, periods, signed reversals, account selections, transfers, party allocations and net/gross controls; pandas operations become scoped SQL or typed C# algorithms. |
| reconciliation_balance_source.py | Independent balances, posting comparisons, adjustments, voucher completeness and supporting evidence; preserve unavailable/empty distinctions. |
| reconciliation_xlsx.py | Financial workbook layouts, literal-text safety, complete evidence, styles and bounded writing; compare cells/totals and peak memory before choosing an approved .NET writer. |
| reconciliation_export_jobs.py | Durable job ownership, scheduling, progress, cancellation, recovery, expiry and private artifacts; local files/OS locks alone do not coordinate hosts. |

Upload controls, Verification status gates and operational exports move with their modules. Future vendor/customer, expense and sales quantitative reports also default to C# but require separate feature briefs. AI/statistical integration is deferred until a concrete need justifies it; no Python runtime is required by the target design.

## 6. Internal report inputs, results and jobs

The browser calls .NET; authorized query services supply typed inputs to C# calculation/export services. Keep these boundaries inside the modular application; no cross-language HTTP protocol, generic dataframe replacement or JSON round-trip is required. Version persisted job/artifact formats when needed, not every in-process call.

| Field group | Contract |
|---|---|
| Identity | Bind report kind, calculation/configuration version and source identity; jobs/artifacts carry an opaque ID and input digest. Reject unsupported persisted formats. |
| Authorized scope | Client, template/TB upload, period bounds, grouping, Treasury view, and explicit account-selection mode. Omitted legacy All follows existing behavior; an explicit empty/incompatible selection never broadens to All. |
| Source identity | Source upload/record revision, selected TB, configuration/context fingerprints, and UTC capture time. All constituent data comes from the same database snapshot. |
| Input datasets | Load only the journal/account/category/assignment/calculation/balance/evidence sets required by the report. Preserve completeness, stable ordering and counts; missing differs from complete-empty. Persisted artifacts additionally need schema/checksum validation. |
| Exact values | Use the approved numeric policy, date-only business values and UTC instants. Raw row_data retains original types/values; public DTO conversions preserve existing wire shapes. |
| Bounded execution | Aggregate/filter in SQL where semantics permit; use bounded C# structures and streaming/private temporary artifacts for large work. Measure end-to-end memory/time, including workbook packaging. Never truncate evidence to meet limits. |
| Snapshot lifetime | Capture complete inputs while holding one read snapshot, then close it before long calculation/export. Never assemble related pages from changing independent snapshots. |
| Result | Bind totals, sections, controls and diagnostics to the captured source/configuration/options. Preserve public report/download contracts and reject substituted scope or incomplete input. |
| Artifact | Record private locator, content type, byte length, checksum, owner/scope, source identity, and expiry. Financial files remain temporary; job metadata never becomes another transaction store. |
| Failure | Use approved public errors and bounded safe diagnostics; no successful report from incomplete inputs. Preserve prior accepted results only with truthful source/freshness labels. |

Source/configuration freshness is part of every result and cache key. Preserve each endpoint's existing current-versus-as-of behavior; never describe mixed snapshots as current. Require an explicit recompute or conflict result when freshness checks fail.

Proposed job states are queued, preparing_inputs, running, succeeded, failed, cancelled and expired; adapt legacy public statuses separately. Persist owner/lease/attempt/progress, bound retries over immutable inputs and handle worker loss. Calculation/export code does not mutate accounting records. Final job/storage limits require P28/P56/P62 approval; heavy export activation waits for P56 recovery/access checks.

## 7. Dependencies and rollback

| Phase band | Entry condition and compatibility bridge |
|---|---|
| P03–P06 | Retain accepted foundations. Shared-schema execution still needs catalog/capacity evidence, ownership and rollback proof. P06 proves explicit legacy/.NET routing with stubs; no Python report-service client. |
| P07A–P07C, before P08 | Agree collaboration rules first; separately approve and rehearse tenant and sharing foundations. Preserve P07 acceptance and existing data; no public switch before proven cross-application authorization. |
| P08–P15 | Tenant membership, engagement assignment, sharing and session/client/role behavior must be consistent across both applications before route switches. Capture response fixtures and verify configuration guards. |
| P16–P29 | Valid client/template/mapping inputs and shared transactions are prerequisites. Settle capacity implementation, hash vectors, coverage/Footing policy, and source revision invalidation before enabling imports. |
| P30–P40 | Preserve existing Verification/evidence references during purge and both consumers during setup changes. P37 brings forward minimum statement/context calculation for profit confirmation; P38 preserves classification/preparation/revision effects; P39 retains vendor workstream assignments read by Cashflows. |
| P53A–P56, after P40 | Execute eight report steps in the plan: inputs/precision, statements, Notes, Cashflows, reconciliation/evidence, APIs/screens, workbooks and jobs/acceptance. Reuse P37's proven helper; compare source controls, legacy results and exported evidence before switching each operation. |
| P41–P45, after reports | Port Audit Rules documents, cross-checks, categories, compilation and historical guards. Keep report categories independent of Verification classifications and preserve shared metadata/revisions. |
| P46–P52 | Rulebook/assignment contracts and evidence IDs must already match. Test frozen histories, concurrent edits, and shared-modal behavior before switching writes. |
| P57–P63 | Reconcile audit/Drive/backup dependencies, full acceptance evidence, deployment/storage limits, and rollback rehearsal before retiring old modules. |

IDs remain stable; execution is P06 → P07 → P07A–P07C → P08–P40 → P53A–P56 → P41–P52 → P57–P63. During coexistence, legacy endpoints retain their existing Python execution/database path until accepted replacements take ownership. P06 is a migration bridge, not a permanent reporting dependency. Pull forward a bounded prerequisite or retain the whole legacy operation when a later dependency blocks safe switching; never introduce an unapproved Python helper service.

Each operation/scope has one active writer. Shadow comparisons use isolated data, not duplicate production imports. Keep shared schemas backward-compatible; reconcile uncertain commits before routing back, never fail over by replaying a write. Rollback never deletes history, drops required columns or overwrites newer unrelated work. Destructive retirement needs protected backups and explicit acceptance/authorization.

## 8. Acceptance fixtures to establish

Expected results come from independent source controls plus the captured existing engine/SQL behavior; unresolved baseline defects are recorded rather than copied or silently fixed. Use synthetic fixtures and approved external workbook copies; never commit client files. These are planned checks, not results already run.

| Set | Required comparisons |
|---|---|
| G01 — API/access | Exact API shapes; expired/inactive sessions, both tenant types, membership, assigned/unassigned staff, configurable sharing/CEO visibility, policy changes, revocation and foreign object IDs. |
| G02 — Cells/amounts | XLS/XLSX native/text/serial dates, formulas/errors, Unicode, leading-zero codes, exact rates, trailing-zero equivalence, blank versus zero, nonfinite input, and row order. |
| G03 — Source controls | Footing differences below/at/above 0.01 and 1.00, confirmed control rows, full coverage, optional fields, rejected exclusions, and empty-period confirmation. |
| G04 — Identity/history | Original/repeated/renamed/reordered inputs; explicit IDs, identical repeated lines, unique corrections, ambiguity, full/partial removal, exact history and receipt counts. |
| G05 — Atomicity/capacity | Expired/altered plans, simultaneous imports/reservations, real rollback, lost commit acknowledgement, and projected peaks just below/at/above 500 MB. |
| G06 — Trial Balance/setup | Hierarchy and code corrections, period overwrite, mappings, independent report/Verification classifications, profit context, and missing balance evidence. |
| G07 — Rules/Verification | Rule compilation/versioning, all status gates, evidence/Forms/procedures/observations, save/verify concurrency, session ranges, and historical result protection. |
| G08 — Financial reports | C# versus independent source controls and legacy Cashflows/Treasury views, selections, periods, gross sides/adjustments, statements/Notes, evidence and workbook cells; include numeric bounds, rounding/overflow, large amounts and repeated fractions. |
| G09 — Jobs/freshness | Authorized tenant/engagement scope and revocation across caches/jobs/downloads; source/config changes, corrupt artifacts, restart, cancellation, expiry and concurrency. |
| G10 — Operations | Current/history exports, guarded purge, restored schema/data/security/sequence checks, credential expiry/reconnect, and legacy URL compatibility. |

P03 establishes the isolated .NET harness; each port phase adds its relevant fixtures, with P61 cross-module acceptance. Existing Python test functions and archived pass counts identify coverage, not a current executable baseline. Performance targets require source-backed timings in comparable environments.

## 9. Acceptance and remaining verification

- C01's 500 MB ceiling is decided; capacity code/helper changes are deferred to their approved phases. No quota, schema, Python source, or live service was changed here.
- Aadil approved this ownership/sequence revision; completed implementation remains accepted. Subsequent implementation, local acceptance and Git steps retain their separate approvals.
- Deployed schema/provider metering remain unverified; local P04 tests do not certify them. Per-route fixtures, authorization, hash vectors, numeric bounds, golden outputs and legacy reachability remain phase work.
- Review C# report ownership, execution order, prerequisite guards, API/value preservation and capacity policy. No local-server test is needed for this documentation revision; implementation phases require their own checks and acceptance.
