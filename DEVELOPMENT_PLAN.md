# Development Plan

This file is the active roadmap for evolving development work. It records current objectives, evidence, decisions, discoveries, approved phases, and next actions. It is updated as new evidence changes the plan.

## Working method

Each phase follows this sequence:

1. Define the objective and evidence required.
2. Confirm the candidate file scope.
3. Approve the implementation approach.
4. Make the smallest scoped change.
5. Verify locally and record the result.
6. Decide whether to continue, revise, or close the phase.

Performance work follows: measure → identify the bottleneck → change one area → retest.

## Current development: Verification and application performance

### Objective

Reduce user waiting time during login, Client Cards loading, Verification loading, procedure saving, and grouped Save/Verify while preserving all existing save, status, cache, and payload behavior.

### Evidence collected

- The grouped endpoint is already functionally batched and uses the corrected snake_case payload.
- Earlier HAR captures showed grouped Save/Verify requests taking approximately 13–18 seconds.
- A later HAR showed `/api/clients` taking approximately 10.69 seconds, mostly in server wait time.
- `/api/auth/current-client` took approximately 1.13 seconds, also mostly in server wait time.
- Static files generally loaded quickly, so browser download speed is not currently the leading explanation.
- Verification requests use `localhost`; distance from the Wi-Fi device should not materially affect browser-to-local-server traffic.
- Remote Postgres/Supabase latency remains a possible contributing factor.

### Important discovery

The frontend can request performance mode by adding `perf=1` to the Verification page, but the grouped endpoint initially lacked matching diagnostics.

An opt-in diagnostic wrapper was added to `routers/verification.py` to expose total request time and database call metrics through `_perf`. The local server also required a UTF-8 console-output fix in `database.py` because Windows `cp1252` encoding caused startup failure on Unicode diagnostic messages.

### Current verification state

- `database.py` and `routers/verification.py` compile successfully.
- The local server starts on port 5000 and the root endpoint returned HTTP 200.
- The existing verification index test passed.
- Browser/server logs confirmed Save/Verify remains approximately 16–20 seconds locally and on Render; detailed component timings were not captured because the deployed request lost `perf=true` during navigation.
- Diagnostics, procedure-check batching, and Verify progress feedback were implemented, tested, committed, and pushed through commit `2d13ba0`.

## Active tracks

### Track A — Verification performance

1. Use the diagnostic page URL below.
2. Perform a representative Save/Verify action.
3. Capture the grouped endpoint response and server-console metrics.
4. Identify whether database time accounts for most of the total time.
5. Optimize only the measured bottleneck in a separate approved phase.

Diagnostic page:

`http://localhost:5000/static/verification.html?type=cash&state=session&id=788&voucher=CPV-0826-145&perf=1`

Expected grouped request:

`http://localhost:5000/api/verification/save_and_verify_voucher_bundle?perf=true`

### Track B — Login and Client Cards performance

1. Completed: measure `/api/auth/current-client` and `/api/clients` separately with authentication.
2. Completed: check duplicate calls, response sizes, and expected authorization paths.
3. Completed: record the local baseline; no office/network comparison is needed unless the issue
   returns in another environment.
4. Deferred: change login or Client Cards only if server-side timing proves a safe material gain.

### Database investigation result

Read-only PostgreSQL `EXPLAIN ANALYZE` checks confirmed that SQL execution itself is not the main delay:

- `latest_cashbook_date` executed in approximately 31.8 ms, but scanned about 60,000 rows and returned about 19,861 rows to the application.
- `cashbooks` executed in approximately 4.5 ms, but returned about 7,859 rows to the application.
- The templates base query executed in approximately 0.07 ms; its user-visible delay is consistent with repeated per-template mapping requests.

The multi-second browser timings are therefore mainly caused by large result transfer/Python JSON processing for Verification discovery endpoints and repeated remote database round trips for template mappings. Query-plan checks required escalated network access because the configured Supabase database is remote.

### Login and Client Cards investigation result

The authenticated baseline used the temporary analyst account against the local server. Login
returned HTTP 200 with a 55-byte response in three fresh runs measuring approximately 3.16, 3.61,
and 1.18 seconds. Authenticated `GET /api/auth/current-client` returned HTTP 200 with 95 bytes in
approximately 0.23–0.24 seconds, and paginated `GET /api/clients?limit=20&offset=0` returned HTTP
200 with 366 bytes in approximately 0.43–0.44 seconds across three runs. The analyst-only
`GET /api/users` correctly returned HTTP 403, confirming the expected role restriction.

The endpoint intentionally performs two reads when pagination is requested: one count and one page
query. Code review found no duplicate Client Cards request in `client_select.js`; its separate
`GET /api/auth/current-client` session guard is expected. The login path is one user lookup, local
bcrypt verification, and two session/user writes; a valid authenticated current-client path is one
session/user join plus one client-summary lookup when a client is selected. The stable post-login
timings and small response sizes do not justify a query or frontend rewrite. Login variability is
consistent with bcrypt plus database/connection wait, but cannot be separated further without
server-side timings. No application-code change is recommended in this phase.

### Verification discovery payload review

Direct local measurements showed that `latest_cashbook_date` returns only 28 bytes but can take
approximately 1.37–8.40 seconds, with the cold call being the outlier. The normal fresh popup now
uses today's date directly and therefore does not call this endpoint; it remains relevant to
date-auto-detection paths such as cashbook restoration. `cashbooks` returned 62 bytes and took
approximately 1.60–2.96 seconds. `preview_cashbook` returned 50 rows of approximately 21.7 KB
from a 130-row result and took approximately 2.30–9.60 seconds.

`audited_cashbook` returned approximately 92 KB for 130 transaction lines and took approximately
0.77–1.15 seconds in direct calls. The lightweight session index returned approximately 31.4 KB
for the same 130 lines and took approximately 0.51–0.53 seconds, but the current restore path
intentionally uses `audited_cashbook` because it needs full row data. In Playwright, the historical
Cashbook popup reached its options in approximately 2.63 seconds, the 50-row preview became visible
in approximately 2.63 seconds, and restoring the first voucher took approximately 3.72–8.45
seconds across runs. The first-voucher waterfall awaits audited data, the session Rulebook, and
`/next`; their browser request timings were approximately 0.98–2.47s, 0.22–4.40s, and 1.78–1.87s.

The payload sizes are not by themselves a safe reason to change response contracts. The next
targeted phase is the first-voucher startup waterfall: measure whether independent requests can be
started earlier without changing Rulebook safety, session state, or first-voucher correctness.

### Fresh Procedure Modal timing review

The approved direct and browser measurements used fresh test sessions and an existing assigned
Category. The server returned HTTP 200 with a response of approximately 2.3 KB. Across fresh
requests, total server time was approximately 2.27–2.47 seconds, with four reads and approximately
0.84–0.95 seconds of measured database time. The named stages were approximately 1.43–1.59 seconds
for shared support data, 0.41–0.46 seconds for voucher checks, and 0.40–0.49 seconds for
cross-check evaluation.

The browser-visible first-open time was approximately 2.55 seconds, with one
`procedure_modal_data?perf=true` request. Reopening the same modal without saving took approximately
62 ms and made no API request, confirming that the client cache and in-flight reuse are effective.
Closing the modal intentionally saves and invalidates the cache, so that path was not treated as a
warm-cache measurement. The current one-request design and small response are sound; no additional
fan-out, cache, or query rewrite is justified without more specific evidence separating connection
wait from Python/response assembly time.

## Phase status

| Phase | Scope | Status | Next action |
|---|---|---|---|
| Baseline review | HAR and prior-chat review | Completed | Continue with measured diagnostics |
| Grouped Save/Verify diagnostics | `routers/verification.py`, with existing database metrics | Implemented and pushed | Use component evidence during optimization |
| Login/Client Cards investigation | Authenticated request timing | Completed; login, session guard, Client Cards, and role restriction measured | Defer optimization unless server-side timing isolates a safe material improvement |
| Verification discovery payload review | `routers/verification.py`, `static/verification.js`, discovery tests | Completed; direct and browser payload/timing baseline recorded | Review the first-voucher startup waterfall |
| Database query-plan investigation | Read-only Postgres `EXPLAIN ANALYZE` | Completed | Reduce Verification result payloads |
| Verification bottleneck optimization | Procedure-modal diagnostics and duplicate sibling-read removal | User-tested and pushed | Continue with the next measured bottleneck |
| Rulebook template metadata wiring | `routers/audit_rules.py`, Verification adapter, Procedure Modal | User-tested and pushed | Continue with measured fresh-data optimization |
| Shared set-based Procedure Modal loader | `routers/verification.py`, performance and Save/Verify tests | Implemented, tested, and pushed | Review only if fresh modal timing remains material |
| Grouped Save/Verify final status computation | `routers/verification.py`, Save/Verify tests | Implemented, tested, and pushed | Review only if Save/Verify timing remains material |
| Audit Rules optional Sortable.js resilience | `static/audit_rules.js` | User-tested and pushed | Continue with the next measured Rulebook/Verification task |
| Procedure Modal duplicate metadata lookup | `routers/verification.py`, performance tests | User-tested and pushed | Continue with the next measured fresh-data task |
| Cross-check configuration read batching | `routers/verification.py`, performance tests | User-tested and pushed | Continue with the next measured bottleneck |
| Grouped Save/Verify sibling resolution reuse | `routers/verification.py`, atomic Save/Verify tests | User-tested and pushed | Continue with the next measured bottleneck |

### Current pendency register — 2026-08-31

| Pendency | Objective | Status |
|---|---|---|
| Authenticated Login/Client Cards baseline | Separate login cost from stable post-login request behavior | Completed; no code change recommended |
| Fresh Procedure Modal timing review | Separate server, database, and browser/cache timing before changing the modal loader | Completed; no code change recommended |
| This phase — Verification discovery payload review | Separate discovery response cost from the first-voucher request waterfall | Completed; no payload contract rewrite recommended |
| First-voucher startup waterfall review | Start independent work earlier only if Rulebook/session safety and correctness remain unchanged | Pending; next targeted phase |
| Audit Workbook Rulebook reuse | Extend the compiled Rulebook approach after Verification remains stable | Later planned phase |
| Login optimization | Separate bcrypt time from database/connection wait before changing code | Deferred |

### Latest cashbook date optimization result

The `latest_cashbook_date` endpoint was changed to fetch distinct dates first and retrieve JSON rows only for candidate dates in descending order. The response remained `{"latest_date":"2026-08-20"}` for client 5/cash.

- Before: approximately 8.34 seconds.
- After: approximately 2.08 seconds after a clean server restart.
- `cashbooks` remains a separate issue and was not changed in this phase; a subsequent run measured approximately 4.84 seconds.

## Decision record

- Treat the phase plan as an adaptive roadmap, not a fixed prediction.
- New evidence creates a separate investigation or branch rather than silently expanding an implementation phase.
- Keep incident fixes, investigations, optimizations, and features separately scoped.
- Do not commit or deploy until the user tests the completed phase and confirms the result.

### Fresh Vouchers date default

Fresh Vouchers/Bankbook entry now requests only the local calendar day. This skips the latest-date lookup and month-wide discovery query for the common case. Existing restored views and persisted ranges remain unchanged; users can manually widen the range when needed.

| Phase | Scope | Status | Next action |
|---|---|---|---|
| Fresh Vouchers date default | `static/verification.js`, `static/verification.html` | Implemented locally | Test today-only load and manual range expansion |

### Save/Verify component diagnostics

The attached HAR confirmed approximately 7 seconds for procedure-modal data and 16.2–16.6 seconds for Save/Verify, but did not include server-console database metrics. The existing opt-in Save/Verify diagnostics now also report line-save, observation-sync, status-engine, procedure-check, voucher-check, verification, and database timings. Next action: repeat one Save/Verify with `perf=1` and capture the server console line.

The tray controller now redirects background server output to `%TEMP%\audit_app_server.log`, so diagnostics can be collected without a visible terminal.

### Verify action progress feedback

The Verify and Verify & Next actions now retain the existing button progress indicator and additionally show a wait cursor and modal busy state until the complete save/verify/navigation sequence settles. The save-before-verify order and backend transaction boundary are unchanged.

### Next mini-phase: Save/Verify logical-stage optimization

The next mini-phase focuses on the backend logical verification stage while preserving the one-click
user actions, save-before-verify ordering, transaction boundary, and cache-update timing. The first
local optimization now skips the intermediate status calculation inside grouped line saves and
keeps the one final status computation after all supplied state is persisted. Persistence/write
optimization remains a separate follow-up mini-phase.

### Planned foundation: controlled configuration lock and session cache

Before targeted procedure-window optimization, add a deliberately simple safety foundation:

- Administrators may open and view Audit Rules and related configuration while verification sessions are active.
- Saving configuration changes that could affect verification is blocked while relevant active verification sessions exist.
- The backend, not only the frontend, enforces this restriction.
- Stable Audit Rules, document requirements, roles, and template metadata may then be cached for the verification session.
- Voucher-specific linked evidence, observations, physical bills, and procedure ticks remain fresh data.

This is necessary for safe session-level caching, but it does not block independent Save/Verify persistence optimization. The planned order is: review affected code, implement the backend lock, add narrowly scoped stable-data caching, measure first and repeat procedure-window opens, then optimize remaining voucher-specific calls.

### Stable template metadata cache

The Audit Rules lock was user-tested on Render. Stable template column metadata is now cached
process-wide for 10 minutes and explicitly invalidated when master columns or template mappings
change. Verification reuses this cache for document grouping and field resolution; voucher-specific
 evidence and the Save-before-Verify sequence remain uncached and unchanged.

### Current decision: Audit Rules bundle before broad modal-query consolidation

The remaining `procedure_modal_data` work is intentionally reordered. Its data groups have
different freshness, historical-resolution, shaping, and business rules, so broad SQL consolidation
could introduce subtle errors. The live linked-document query plan also showed database execution
around 0.12 ms; most observed delay is connection and remote round-trip latency.

The next phase is a versioned Audit Rules bundle for Verification:

- Prepare one stable bundle containing Categories, Documents, Cross-check configuration, authority
  roles, document requirements, templates, and mappings.
- Cache the bundle by version and invalidate it after Audit Rules/template changes.
- Capture the active bundle version when a verification session starts.
- Reuse the bundle in Verification and the procedure modal.
- Keep linked documents, procedure ticks, observations, physical bills, and calculated results fresh.
- Re-measure `procedure_modal_data` before optimizing any fresh-data query.

Current measurements: `procedure_modal_data` is approximately 2.9-3.1 seconds locally, with 12-13
database calls. Cross-check configuration caching reduced the repeat cross-check stage from about
961 ms to about 719 ms. The broad parallel-query experiment was reverted after it increased latency
through connection-pool contention. The targeted linked-document index was added to both schema
paths, but its user-visible effect was not material in the small test case.

The next controlled measurement found that the five parallel modal helpers independently repeated
the same sibling-line lookup: 21 reads and 44.1 seconds of cumulative database time. The smallest
safe fix resolves siblings once in the parent request and seeds each worker's request-local memo.
The same live request then returned HTTP 200 in 4.32 seconds with 13 reads and 12.3 seconds of
cumulative database time. The response remained 1,616 bytes, confirming that the improvement is in
database round trips rather than payload reduction. Focused regression tests now pass (15 total).

The session Rulebook now also carries ordered active template-column metadata. Verification exposes
the loaded packet through its Procedure Modal adapter, and the modal uses that metadata before its
legacy `/api/templates/{id}` fallback. A local browser check opened the modal successfully with zero
template-detail requests. The cache is reset when the Rulebook version changes, so a new session
cannot reuse column metadata from a different stable configuration.

The user HAR confirmed the current assets (`procedure_modal.js?v=15`, `verification.js?v=26`), 20 API
requests with HTTP 200, and no `/api/templates/{id}` calls. Procedure Modal time was approximately
3.7 seconds; Save/Verify remains approximately 10.6 seconds and is the next measured bottleneck.

The template/cache/diagnostics phase was pushed in commit `f00d8dc`. Later local changes remain
uncommitted until user testing and confirmation. Do not commit or push automatically.

### Shared set-based Procedure Modal loader

The Procedure Modal endpoint now reuses the existing set-based voucher-support loader for linked
documents, procedure ticks, observations, and Physical Bills instead of invoking five helper reads
through a thread fan-out. Cross-check evaluation reuses the linked-document IDs already fetched;
its evidence-field resolution rules remain unchanged. The response contract and standalone helper
endpoints remain compatible, and no voucher-specific data is cached.

Focused Verification tests passed (14); the full suite passed (47) with four existing warnings.
Python compilation passed. Manual timing comparison and user confirmation remain pending before
any Git staging or commit.

### Cross-check configuration read batching

On a Cross-check configuration cache miss, `_get_category_cross_check_configs()` previously ran
one document-chain query for every active Cross-check attached to a Category. It now performs one
batched document-chain query for all those Cross-check IDs, grouping the rows back in the existing
`sort_order` and response shape. Cache behavior, chain ordering, and evaluation rules are unchanged.

The focused performance file now passes 5 tests, including a two-rule fixture that asserts exactly
one document-chain query. The full suite passes 48 tests with four existing warnings, and Python
compilation passes. A live read-only modal request returned HTTP 200 with four database reads; the
current test Category has no active Cross-check chain, so populated-chain live timing remains a
manual follow-up. The user manually confirmed the Procedure Modal, Save/Verify, cache invalidation,
and navigation flow; this phase is ready for Git lock.

### Grouped Save/Verify sibling resolution reuse

The grouped Save/Verify endpoint now resolves each eligible line's sibling IDs once inside the
existing transaction and reuses that list for both line saving and final Cleared/Query status
calculation. This removes the duplicate sibling-resolution read while preserving atomic rollback,
fresh status computation after all support data is saved, and the existing response contract.

Focused Grouped Save/Verify tests pass 11 tests, including a regression assertion that the same
sibling list reaches both stages. The full suite passes 49 tests with four existing warnings, and
Python compilation passes. The user manually confirmed session start, grouped Save/Verify,
cache invalidation, and line navigation; this phase is ready for Git lock.

## Planned redesign: Audit Rules Rulebook

This is the approved future roadmap for redesigning the Audit Rules module. It does not claim that
any redesign phase has started or that current behavior should change immediately. The existing
Audit Rules tables remain the authoritative editing model until a replacement is proven.

### Design direction

Verification needs a category-first, client-aware, versioned Rulebook rather than repeatedly
assembling the administration model while processing vouchers. The compiled Rulebook should contain:

- the client account-code-to-category index;
- category profiles with supporting documents, authority roles, signature requirements,
  cross-check chains, and working-paper gate settings;
- global voucher-level checks;
- active template and mapping metadata; and
- a rulebook/configuration version.

Stable configuration may be cached. Linked documents, procedure ticks, observations, physical bills,
cross-check results, and calculated Cleared/Query results must remain fresh. Existing safeguards must
remain: Audit Rules locking during active verification, category freezing and matrix versions,
historical sibling-session behavior, Physical Bill handling, and atomic Save → Verify behavior.

### Phase-wise roadmap

#### Phase 0 — Baseline and design specification

Inventory every Audit Rules field consumed by Verification, including the Verification grid,
procedure modal, Save/Verify status engine, and Audit Workbook. Record the current read paths,
database-call counts, and timings for session start, first voucher, modal open, and Verify. Produce a
field-ownership/workpaper mapping, call graph, Rulebook contract, cache/version ownership diagram,
and compatibility notes. Decide whether account-category assignments are part of the global bundle
or a client-specific companion cache. No implementation begins until the contract reflects the real
intern workflow.

#### Phase 1 — Read-only Rulebook compiler

Build a backend compiler that assembles active categories, documents, requirements, roles,
signature specimens, cross-checks, voucher checks, account assignments, templates, and mappings.
Add clear warnings for incomplete configuration, such as inactive template references, invalid
cross-check documents, missing specimens, or unmapped comparison fields. Compare its output with
current endpoint results without changing frontend or Verification behavior. The compiler must use
a bounded number of queries and must not include fresh voucher evidence.

#### Phase 2 — Versioning, caching, and invalidation

Add a process-level Rulebook cache, monotonic configuration version, and hit/miss diagnostics.
Invalidate the correct global or client-specific cache after document, cross-check, category,
requirement, voucher-check, template/mapping, signature-specimen, or account-assignment changes.
Keep global configuration, client assignments, and the combined session snapshot independently
keyed where appropriate. Prove that known mutations cannot leave stale configuration.

#### Phase 3 — Verification session snapshot

Capture the active Rulebook version or snapshot identifier when a verification session starts.
Define behavior for sessions created before this feature and preserve existing session restoration.
Stable configuration must remain consistent throughout a session, while evidence and calculated
results remain live. Existing Audit Rules locking remains the safety boundary.

#### Phase 4 — Local account/category resolution

Load the client account index once and resolve account heads locally during voucher rendering.
Preserve category freezing, unresolved-account fallback, category IDs/names, and invalidation after
assignment changes. Remove redundant remote reads only after measurements prove the Rulebook path is
correct and faster.

#### Phase 5 — Rulebook use in the procedure modal

Use the relevant category profile or Rulebook reference for stable document, role, template, and
cross-check requirements. Keep linked evidence, procedure ticks, observations, physical bills,
current status, and calculated results fresh. Preserve historical sibling-session rules, modal
navigation, local Save behavior, and atomic Save → Verify → cache update → navigation. Confirm that
modal opening requires fewer configuration calls without changing what the intern sees.

#### Phase 6 — Explicit four-gate working-paper output

Define and expose the four working-paper gates consistently in backend and frontend terminology:
Approval, Supporting Documents, Account-head Classification, and Observation. Ensure final
Cleared/Query rollups, query reasons, and printed/exported output use the same underlying gate
results. This phase is for clarity and consistency; audit conclusions must not change without an
explicit decision.

#### Phase 7 — Category-first administration experience

Redesign the primary Audit Rules workflow around Category profiles: account classification,
supporting documents, required signatures, cross-check chains, voucher-level checks, and validation
warnings. Show related account codes, ordered cross-checks, incomplete configuration, and change
impact in one coherent view. Retain current entity CRUD views and endpoints as advanced/reference
compatibility surfaces until the new workflow is proven.

#### Phase 7 status — roadmap reconciliation

The Category-first Audit Rules administration experience is already implemented locally in the
current Audit Rules code. Categories have a primary rail/list/detail workflow with account
classification context, attached Documents, Cross-checks, Cleared-requirement summaries, and
validation warnings. Existing standalone entity CRUD views and endpoints remain available as
reference/compatibility surfaces. This entry reconciles the roadmap and introduces no new behavior.

Deferred follow-up: the Procedure Modal's Audit Procedures tablet may not visually refresh its
count/ring/color immediately after closing in every browser path. This remains a separate UI fix.

Next phase boundary: do not begin Phase 8 controlled data-model cleanup until the existing Phase 7
workflow has been formally user-tested and confirmed.

#### Phase 8 — Controlled data-model cleanup

Only after the Rulebook and administration workflow are proven, clarify or retire legacy status
fields, resolve overlapping transaction-approval and authority concepts, add explicit requirement
types or constraints where needed, and retire redundant endpoints. This is the final phase because
schema cleanup and endpoint removal are the least reversible changes.

### Redesign guardrails

Do not cache voucher-specific evidence or permanent calculated results, replace the existing tables
before the compiler is proven, remove historical behavior, allow category changes to reinterpret
frozen lines, merge all modal queries into one untested function, or introduce a competing
Save/Verify path. Each phase requires its own approved file scope, implementation approval, measured
verification, user testing, and confirmation before any Git staging or commit.

### Next action

Start with Phase 0 as a read-only study. The first deliverable is the field-ownership matrix,
Verification read-path inventory, Rulebook contract draft, cache/version ownership decision, and
baseline measurements. No application code changes are included in Phase 0.

### Phase 0 study findings (read-only baseline)

#### Current ownership and data shape

- Global rule entities are stored in `audit_rule_categories`, `audit_rule_documents`,
  `audit_rule_document_fields`, `audit_rule_document_auth_roles`, `audit_rule_cross_checks`,
  `audit_rule_cross_check_documents`, `audit_rule_category_documents`,
  `audit_rule_category_cross_checks`, and `audit_rule_voucher_checks`.
- Client-specific account classification is stored separately in
  `account_category_assignments(client_id, account_code, category_id)`.
- Client-specific expected signatories are stored in `signature_specimens`, joined by client,
  document, and role.
- `audit_rule_categories.matrix_version` protects category/document requirement interpretation;
  `verification_details.matrix_version_applied` records the version used by a line. It is not a
  complete global configuration version.
- `verification_sessions` has no Rulebook or configuration snapshot identifier.

#### Current read paths

| Consumer | Stable-rule reads | Fresh-data reads |
|---|---|---|
| Verification grid/session | categories, category-for-account, templates/mappings, voucher checks | voucher lines, evidence, line status, observations |
| Procedure modal | category guidance, documents, roles, cross-check definitions, templates/mappings | linked documents, procedure ticks, observations, Physical Bills, current results |
| Save/Verify status engine | category/matrix version, required documents, roles/authorities, specimens, cross-check configuration | evidence links, ticks, observations, calculated gates |
| Audit Workbook | batched account/category resolution, category names, template metadata | workbook rows and current line status |
| Audit Rules administration | separate CRUD list/detail responses for each rule entity | not applicable |

`verification.js` currently calls category resolution, guidance, voucher-check, template, and status
endpoints separately. `procedure_modal.js` consumes guidance while loading live evidence/status data.
`audit_workbook.js` consumes the workbook endpoint rather than administration endpoints.

#### Current cache and invalidation baseline

`routers/audit_rules.py` uses a process-level in-memory cache with a ten-minute TTL and explicit full
invalidation for most Document, Cross-check, and Category writes. Account-category lookups use a
separate client-prefixed cache and client-scoped invalidation. There is no monotonic Rulebook version,
compiled category-profile cache, or session snapshot cache.

Important gaps requiring explicit redesign decisions:

- Signature-specimen create/update/delete/restore paths do not use the active-verification lock or
  central Audit Rules invalidation, although specimens affect the approval gate.
- Cross-check changes invalidate the general cache but deliberately do not bump Category
  `matrix_version`; a future Rulebook version must distinguish these concepts.
- Template/mapping invalidation belongs to the existing stable-template metadata cache and must not
  be duplicated by a competing cache owner.
- Category guidance is assembled through multiple helper queries, including per-document role/field
  reads and per-cross-check document reads. This is the main compiler/query-count baseline.

#### Phase 0 Rulebook contract draft

The first compatible read model should conceptually be:

```json
{
  "global_version": 0,
  "client_id": 0,
  "account_category_index": {"account-code": {"category_id": 0, "category_name": ""}},
  "categories": [{
    "category_id": 0,
    "name": "",
    "matrix_version": 1,
    "kb_overview": "",
    "documents": [{
      "document_id": 0,
      "name": "",
      "document_type": "linked_template",
      "template_id": 0,
      "required": true,
      "transaction_level_approval": false,
      "risk_rating": null,
      "fields": [],
      "authority_roles": [],
      "signature_requirements": []
    }],
    "cross_checks": [],
    "working_paper_gates": {
      "approval": {},
      "supporting_documents": {},
      "account_head_classification": {},
      "observation": {}
    }
  }],
  "voucher_checks": [],
  "templates": [],
  "warnings": []
}
```

This is a design draft, not an API commitment. The likely ownership is a global compiled
configuration bundle plus a client-specific account/signature companion, combined into a
session-level snapshot. The snapshot must contain stable rules only; evidence and calculated
outcomes remain outside it.

#### Phase 0 decisions

1. Keep existing relational tables and CRUD contracts authoritative during the redesign.
2. Build the Rulebook as a read-only compatibility layer and compare it with current endpoint
   responses before migrating consumers.
3. Treat global rules, client account assignments, and client signature specimens as separate
   invalidation domains, with a combined session snapshot at the consumption boundary.
4. Extend the lock/invalidation audit to signature specimens and template/mapping mutations before
relying on session-level stable configuration.

### Phase 2 status — versioning, cache ownership, and invalidation

Implemented locally in `routers/audit_rules.py` and `routers/templates.py`, with tests extended in
`tests/test_audit_rulebook.py`.

- Added a process-local monotonic Audit Rules configuration version.
- Made existing Audit Rules cache entries expire when the configuration version changes, in
  addition to the existing TTL behavior.
- Added missing invalidation for voucher-check create/update/delete/restore/reorder operations.
- Added active-verification locking and invalidation for signature-specimen create/update/delete/
  restore/hard-delete operations.
- Coordinated template/master-column cache invalidation with the Audit Rules configuration version
  through the existing `invalidate_template_cache()` owner.
- Preserved client-scoped account-category cache invalidation and category-level `matrix_version`
  semantics.

Verification completed:

- `./venv/Scripts/python.exe -m pytest tests/test_audit_rulebook.py -q` — 3 passed.
- `git diff --check` — passed.

The focused test run reported only the existing `.pytest_cache` environment warning. Full-suite
failures recorded before this phase remain outside this phase's changed consumers:
`test_verification_atomic_save_verify.py` expects the old response without diagnostic
`_perf_components`; no Verification file was changed here.

User testing checklist:

- Open Audit Rules while a verification session is In Progress; confirm pages remain viewable.
- Attempt a voucher-check or signature-specimen save during an active session; confirm the server
  rejects the mutation with HTTP 409.
- After ending active sessions, edit a voucher check and confirm a subsequent Rulebook read reflects
  the change immediately.
- Edit a template or master column and confirm the next Rulebook/template read reflects the change.
- Confirm normal Trial Balance category assignment still updates only the affected client's cache.

Commit/push: not required yet. Aadil should test the local server and explicitly approve before any
staging, commit, or push. The next proposed phase is Phase 3: capture a Rulebook version/snapshot
identifier when a Verification session starts.

#### Phase 2 user confirmation

Aadil manually checked the Verification process, opened the procedure window, and verified
transactions after the Phase 2 changes. The workflow was confirmed good and working. Phase 2 is
therefore user-tested and ready for a later, separately approved Git action; no commit or push has
been requested yet.

## Practical safety phases before further Rulebook redesign

The earlier idea of releasing the Audit Rules lock only when a Verification session becomes
Completed is impractical for normal work. Interns must be able to leave an unfinished session,
change configuration safely, and later resume that session.

### Practical session-lock design

Keep business session status independent from active user presence:

- `In Progress` means unfinished work that may be resumed.
- `Completed` means all work is finished.
- `Cancelled` means the session was intentionally abandoned.
- A separate active-presence/lease record means a user is currently inside Verification.

Audit Rules mutations remain blocked while any valid Verification presence is active, regardless of
which user owns it. Leaving Verification releases that user's presence without completing the
business session. A short heartbeat lease handles an unexpected browser close. Re-entry can resume
the unfinished session after the lock has been released.

Every configuration change should remain auditable, including the actor, timestamp, previous/new
Rulebook versions, and the possibility that unfinished sessions may see changed rules. On re-entry,
the user should receive a clear Rulebook-change notice. Previously frozen categories and finalized
evidence must not be silently reinterpreted; unfinished work may use the updated configuration under
the agreed compatibility rules.

### Practical Verification status rule

Use one clear three-state rule:

- `Pending`: the line/voucher has not been touched.
- `Cleared`: it has been touched and every required rule passes.
- `Query`: it has been touched and one or more required rules fail.

For any meaningful procedure, evidence, observation, physical-bill, or voucher-check change, the
safe order is: persist the change, recompute the affected line(s), persist the new Cleared/Query
status, return the updated status, and refresh the visible badge. A deselected required tick must
therefore change a previously Cleared touched line to Query; an untouched line must remain Pending.

### Revised safety phase order

#### Safety Phase A — active Verification presence and practical Audit Rules locking

Implemented locally in `database.py`, `routers/verification.py`, `routers/audit_rules.py`, and
`static/verification.js`, with the existing focused tests retained in
`tests/test_audit_rulebook.py`.

- Added a separate `verification_presence` store in SQLite and PostgreSQL for active-presence
  leases; business sessions remain resumable and `In Progress`.
- Added authenticated enter, heartbeat, and leave endpoints; the browser renews the lease every
  30 seconds and releases it when returning to the Cashbook, with a best-effort unload release.
- Audit Rules changes are blocked only while a non-expired Verification presence exists, allowing
  unfinished sessions to be resumed after the rules are safely changed.

Verification completed:

- `./venv/Scripts/python.exe -m pytest tests/test_audit_rulebook.py -q` — 6 passed.
- `node --check static/verification.js` — passed.
- `git diff --check` — passed.

Manual local-server verification remains required for multi-user blocking, explicit leave, stale
lease expiry, and resume behavior before this safety phase is considered user-tested.

#### Safety Phase A usability refinement — safe navigation prompt

Implemented locally in `static/verification.js` and `static/client_context.js`. Home, Up, and
Change Cashbook now use one confirmation flow while an active Verification session is present.
After confirmation, pending voucher work is saved, the active-presence lease is released, and the
requested navigation continues. Cancelling keeps the user in Verification. The unfinished session
remains resumable. Browser refresh/close continues to rely on best-effort unload handling and lease
expiry because browsers cannot reliably show an interactive confirmation during unload.

Verification completed:

- `node --check static/verification.js` — passed.
- `node --check static/client_context.js` — passed.
- `git diff --check` — passed.

Commit/push: not required yet. Aadil should test the local server and explicitly approve before any
staging, commit, or push.

#### Safety Phase B — status recomputation after meaningful changes

**In progress.** The atomic voucher-bundle save now persists procedure and voucher-check changes
before recomputing affected line statuses, and returns the refreshed current-line status. Remaining
work is to confirm the full Pending/Cleared/Query transition matrix, including all modal save paths,
and add targeted regression coverage.

The complete requirement remains to ensure procedure/evidence/observation/physical-bill/voucher-check
changes are persisted before Cleared/Query is recalculated. Ensure untouched lines remain Pending,
touched failing lines become Query, and passing touched lines become Cleared. Return and refresh
updated statuses consistently across the table, status explanation, and Procedure Modal.

### Safety Phase B UI follow-up — Procedures statistics refresh

The Procedure Modal close/save path now invalidates the support cache and explicitly refreshes the
Procedures cell for every line in the current voucher. This updates the count, ring, and color
immediately after closing instead of requiring a full page refresh. Local browser verification
remains required.

The modal's Audit Procedures section now also derives a pending/completed visual state from its
required roles: any unchecked role uses the neutral grey treatment, while all checked roles retain
the completed green treatment.

The Verification and Audit Workbook HTML cache-busting versions were incremented so browsers load
the updated Procedure Modal and Verification scripts instead of retaining stale cached JavaScript.

Deferred post-performance phase: Cross-check Field-to-Compare configuration. When selected
Documents have no eligible fields configured, the editor correctly shows no eligible fields. A
separate approved phase will later cover field setup, dropdown population, validation, and
persistence.

The existing Rulebook redesign phases resume only after Safety Phases A and B are user-tested and
confirmed. These safety phases take precedence over the previously proposed Phase 7 administration
redesign and later data-model cleanup.

### Phase 5 status — Procedure Modal category profile separation

Implemented locally in `static/procedure_modal.js`. The existing cached `audit_guidance` response is
now explicitly retained in modal state as `rulebookProfile`, representing the stable category
profile. The existing `procedure_modal_data` response continues to provide fresh linked documents,
procedure checks, observations, Physical Bills, cross-check results, and voucher checks.

This deliberately reuses the established adapter and guidance-cache boundary instead of introducing
a second backend fan-out or duplicate Rulebook fetch. The modal's professional layout and existing
visual styling are unchanged. No live Verification behavior or response contract was removed.

Verification completed:

- `node --check static/procedure_modal.js` — passed.
- `git diff --check` — passed.

User testing checklist:

- Open a categorized voucher's Procedure Modal and confirm all expected document tabs appear.
- Confirm linked evidence still loads and reflects current server data.
- Confirm procedure ticks and observations still load after closing and reopening the modal.
- Confirm Physical Bills and cross-check result sections still display normally where applicable.
- Save a modal change, reopen it, and confirm the saved state is fresh rather than stale.
- Confirm the existing modal layout, spacing, colors, and controls remain consistent with the app.

Commit/push: not required yet. Aadil should test the local server and explicitly approve before any
staging, commit, or push. The next proposed phase is Phase 6: explicit four-gate working-paper
output.

### Phase 6 status — explicit four-gate working-paper output

Implemented locally in `routers/verification.py`, `static/verification.js`, and
`static/procedure_modal.js`, with focused tests in `tests/test_audit_rulebook.py`.

- Added backend grouping from the six existing detailed checks into Approval, Supporting
  Documents, Account-head Classification, and Observation.
- Preserved every detailed check and its explanation beneath the appropriate working-paper group.
- Kept Cleared/Query calculation based on the existing underlying checks; no audit conclusion rule
  was changed.
- Updated the Verification status popover and Procedure Modal validation panel to prefer the four
  grouped gates while retaining a compatibility fallback to the detailed list.
- Kept Observation informational only; no new Observation blocking rule was introduced.
- Printed/exported output was intentionally not changed in this phase because its layout requires a
  separate confirmed brief.

Verification completed:

- `./venv/Scripts/python.exe -m pytest tests/test_audit_rulebook.py -q` — 6 passed.
- `node --check static/verification.js` — passed.
- `node --check static/procedure_modal.js` — passed.
- `git diff --check` — passed.

User testing checklist:

- Open the Verification status explanation and confirm exactly four working-paper groups appear.
- Open the Procedure Modal Validate panel and confirm the same four group names appear.
- Create or view a line with a missing approval requirement and confirm it appears under Approval.
- View a line with missing evidence or cross-check issues and confirm details appear under Supporting
  Documents.
- View an unclassified account and confirm Account-head Classification explains the existing reason.
- Confirm Cleared/Query outcomes remain the same as before this display change.
- Confirm the existing modal spacing, colors, typography, and controls remain professional and
  consistent with the app.

Commit/push: not required yet. Aadil should test the local server and explicitly approve before any
staging, commit, or push. The next proposed phase is Phase 7: category-first Audit Rules
administration experience.

#### Phase 5 user confirmation

Aadil manually verified the Procedure Modal after the category-profile separation and confirmed
everything is fine and working. Phase 5 is user-tested and ready for a later, separately approved
Git action; no commit or push has been requested yet.
5. Do not use `matrix_version` as the global Rulebook version; it remains category-level protection
   for frozen verification lines.

### Phase 1 status — read-only Rulebook compiler

Implemented in `routers/audit_rules.py` with isolated coverage in
`tests/test_audit_rulebook.py`. `compile_verification_rulebook(client_id)` now performs batched
stable-rule reads and assembles category profiles, document fields/roles, authority-backed
signature requirements, cross-check chains, voucher checks, active templates/mappings, and the
client-specific account-category index. It emits warnings for inactive template references,
inactive relationship references, and account assignments pointing to inactive categories.

The compiler is deliberately not cached, not exposed as a live consumer endpoint, and not called
by Verification or frontend code. Voucher evidence, procedure ticks, observations, linked
documents, and calculated outcomes are excluded from the Rulebook.

Verification completed:

- `./venv/Scripts/python.exe -m pytest tests/test_audit_rulebook.py -q` — 2 passed.
- `git diff --check` — passed.

The pytest run reported an environment warning because the existing `.pytest_cache` path could not
be recreated; this did not affect test execution. User testing is required before staging or
committing. The next proposed phase is Phase 2: Rulebook versioning, cache ownership, and
invalidation design/implementation.

### Phase 3 status — Verification session Rulebook snapshot

Implemented locally in `database.py` and `routers/verification.py`, with focused coverage in
`tests/test_audit_rulebook.py`.

- Added nullable `verification_sessions.rulebook_version` to the SQLite and PostgreSQL schema
  paths, including an idempotent upgrade for existing databases.
- Captured the current process-local Audit Rules configuration version when a new Verification
  session is created.
- Preserved historical-session compatibility: existing sessions with `NULL` continue through the
  current behavior and are not retroactively assigned a version.
- Kept evidence, linked documents, procedure ticks, observations, and calculated results outside
  the snapshot; this phase records identity only and does not change consumer behavior.

Verification completed:

- `./venv/Scripts/python.exe -m pytest tests/test_audit_rulebook.py -q` — 4 passed.
- `git diff --check` — passed.

User testing checklist:

- Start a new Cash Verification session and confirm it opens and loads transactions normally.
- Start a new Bank Verification session, if available, and confirm it also opens normally.
- Refresh or navigate within a new session and confirm the session remains usable.
- Complete a transaction and confirm evidence, procedure ticks, observations, and Cleared/Query
  behavior are unchanged.
- Confirm an older existing session, if available, remains accessible without an error.

Commit/push: not required yet. Aadil should test the local server and explicitly approve before any
staging, commit, or push. The next proposed phase is Phase 4: use the Rulebook for local
account/category resolution.

### Phase 4 status — local account/category resolution

Implemented locally in `routers/audit_rules.py` and `routers/verification.py`, with focused tests in
`tests/test_audit_rulebook.py`.

- Added `load_client_account_category_index()` as the shared batched resolver.
- Preserved the existing precedence: latest parsed Trial Balance first, persistent account
  assignments as fallback, and explicit no-match values for unknown accounts.
- Routed the Audit Workbook's distinct-account resolution through the shared loader, reducing the
  need for separate resolution logic.
- Kept the single-account category API contract and frontend behavior unchanged.
- Preserved client isolation and existing frozen-category behavior; no category is retroactively
  applied to an already-frozen verification line.

Verification completed:

- `./venv/Scripts/python.exe -m pytest tests/test_audit_rulebook.py -q` — 5 passed.
- `git diff --check` — passed.

User testing checklist:

- Open a Verification session with assigned accounts and confirm Category values still appear.
- Check an account present in the latest Trial Balance and confirm its Trial Balance Category wins.
- Check an account absent from the latest Trial Balance but present in persistent assignments and
  confirm the fallback Category appears.
- Check an unknown/unassigned account and confirm the procedure window remains unavailable with the
  existing clear no-category behavior.
- Reopen a previously saved/frozen line and confirm later account-assignment changes do not change
  its frozen Category.

Commit/push: not required yet. Aadil should test the local server and explicitly approve before any
staging, commit, or push. The next proposed phase is Phase 5: use category profiles in the procedure
modal while keeping voucher-specific data fresh.

#### Phase 4 user confirmation

Aadil manually checked the updated account/category resolution and confirmed it is working. Phase 4
is user-tested and ready for a later, separately approved Git action; no commit or push has been
requested yet.

#### Phase 3 user confirmation

Aadil opened Bank vouchers successfully. The procedure window remained unavailable because the
related bank account codes had not yet been assigned Categories, which is the expected existing
business rule. Phase 3 behavior was confirmed good and working.

## Approved architecture addition — compiled, versioned Rulebook

The speed strategy will use a shared compiled Rulebook rather than making Verification repeatedly
join Trial Balance assignments to Audit Rules during every voucher-line load.

### Ownership and safety

- Trial Balance remains the source of truth for account-code-to-Category assignments.
- Audit Rules remains the source of truth for Category procedures, evidence requirements,
  authority roles, cross-checks, and status criteria.
- A shared read-optimized Rulebook layer combines both sources by client and Rulebook version.
- Verification fetches the compiled Rulebook once per session and resolves Categories locally.
- A session keeps the Rulebook version captured when it starts, so later Rulebook edits cannot
  silently change an active session's results.
- Assignment or Audit Rules mutations invalidate the affected client's compiled Rulebook; the next
  request rebuilds it from current source data.
- The cache is lazy/versioned rather than startup-only, preventing stale copies across workers,
  restarts, and multiple users.

### Planned implementation phases

1. Build and test the compiled client Rulebook loader and version key.
2. Add one-session Rulebook retrieval and preserve existing category-freeze behavior.
3. Resolve voucher-line Categories and procedures from the session Rulebook in memory.
4. Evaluate procedure ticks and Cleared/Query/Pending criteria from the same Rulebook batch.
5. Measure Network requests, response size, first-voucher display time, and procedure-window time
   before and after each step.
6. Only after Verification is stable, apply the same shared Rulebook/cache approach to the Audit
   Workbook and future Cash/Bank/module cards.

This architecture addition is now the boundary for the remaining speed work. It replaces the
earlier idea of changing Audit Rules into the owner of Trial Balance data: ownership stays separate,
while Verification receives one prepared, versioned packet.

### First implementation pendency completed

The backend now provides a version-aware client Rulebook loader. It reuses the compiled stable
configuration by client and Audit Rules configuration version, clears client Rulebooks after
client-specific category-assignment changes, and clears all Rulebooks after global configuration or
template/mapping changes. The compiler uses the shared Trial Balance-first account/category
resolver. Verification and frontend consumers remain unchanged. The one-session Rulebook
retrieval phase is now complete; the next phase resolves voucher-line Categories and procedures
from the session Rulebook in memory.

### One-session Rulebook retrieval completed

Verification now exposes a session-scoped Rulebook endpoint. It returns the prepared packet for a
matching captured version, preserves legacy sessions with no captured version, and rejects a
session whose captured version no longer matches the active configuration. Frontend consumers are
still unchanged. The next phase is local voucher-line Category and procedure resolution.

### Local Rulebook Category and procedure resolution completed

Verification now resolves account Categories and stable procedure guidance from the session Rulebook
in memory. The existing guidance shape is preserved for the Procedure Modal, including role-name
compatibility. Fresh support data and atomic Save → Verify behavior remain unchanged. A schema alias
also corrected the compiler's template-name query for the live Postgres schema. The next phase is
evaluating procedure ticks and Cleared/Query/Pending criteria from the same Rulebook batch.

### Rulebook-backed procedure and status evaluation completed

Stable required-document and voucher-check definitions now come from the session Rulebook during
status evaluation and voucher verification. Live procedure ticks, evidence, observations, Physical
Bills, cross-check results, and calculated outcomes remain fresh. Rulebook loading occurs before the
atomic Save → Verify transaction to avoid nested pooled-connection pressure. The next phase is
performance measurement and review of any remaining stable-rule reads.

### Rulebook performance baseline

Live local measurements show the session Rulebook response at approximately 15.5 KB: cold reads
took 3.1–5.7 seconds and warm cached reads about 0.5 seconds. The fresh Procedure Modal response
was approximately 2.6 KB but took 8.9–20.6 seconds across repeated reads, confirming that fresh
modal data—not Rulebook delivery—is the remaining dominant bottleneck. The next phase investigates
the `procedure_modal_data` call graph without caching live evidence or results.

### Local tray launcher reliability completed

The tray controller now launches Uvicorn directly through the project virtual environment with the
project folder as its working directory. This prevents an older `run.bat` process from being treated
as the active server and ensures local startup reads the current `.env`. Manual verification confirmed
successful login, session start, presence, procedure/support loading, Save → Verify, and navigation.
The next phase is to inspect the remaining `procedure_modal_data` bottleneck.

### Standalone line-save Rulebook error closed

The standalone `save_line_items` path had a stale function name for session Rulebook loading,
causing status recomputation to raise a `NameError` and return HTTP 500. The reference was corrected,
and the focused regression suite now passes. A fresh manual network trace recorded 42 successful API
requests, including line saves and Save → Verify. Performance optimization remains a separate phase.
