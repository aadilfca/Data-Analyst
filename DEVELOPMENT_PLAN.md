## Current work — Trial Balance assignment workspace — 2026-10-31

| Phase | Status / objective |
|---|---|
| 1 — Foundation | Accepted by Aadil; release authorized: workflow selector, read-only columns, shared scoped reads, independent filters and existing ACC setup/return links. |
| 2 — Financial Statements/Notes | Pending: in-workspace editors/setup and enforced category-to-note relationship. |
| 3 — Cashflows | Pending: in-workspace category, Treasury and source setup; retain historical accounts and refresh behavior. |
| 4 — Verification | Pending: in-workspace workstream assignment/setup, preserving routing and Audit Rules categories. |
| 5 — Integrated review | Pending: prove assignment-to-report freshness, context retention and cross-module result parity. |
| 6 — Cutover | Pending: remove standalone ACC screens, navigation and obsolete code after accepted TB feature parity. |

Assignments retain client/account-code scope. Phase 1 uses existing ACC setup screens; later workflow phases move editors in place. Each phase requires local acceptance and separate Git authorization. Details: CHANGELOG_ARCHIVE.md, “Trial Balance assignment workspace foundation”.

## Full database backups — completed — 2026-10-25

| Phase | Status / objective |
|---|---|
| 1 — Create copies | Accepted: successful full backup, local/Drive checksum and privacy checks, processing display and safe retries; release authorised. |
| 2 — Prove recovery | Accepted: Aadil tested the backup; isolated recovery verified 89 application tables, 255,367 records, 80 sequences, settings/object permissions and 49 financial control groups against the captured source state. |
| Closeout | Phase 1 and Phase 2 are pushed (`98c2c18`). No further backup development phase is planned. |

Setup retains its existing backup entry; Phase 2 is an operator-only local rehearsal. No working-table writes, app restore button, automatic schedule or retention deletion. Source workbooks/Storage objects/global roles/environment credentials remain separate.

Recovery uses protected local evidence and existing PostgreSQL tools. Compact checks transfer complete-table fingerprints, not rows; older backups pass current-state comparison only when every checked section matches. Managed Supabase services and rendered report/export parity are not certified here. See CHANGELOG_ARCHIVE.md, “Isolated backup recovery proof — Phase 2”.

## Cashflow reconciliation gate — 2026-10-22

C1 mappings and signed customer/vendor reversals are accepted. C4–C6 are accepted/committed (`223a1ce`, `bfa7731`, `fa6be37`); January–June figures remain the baseline. Details: CHANGELOG_ARCHIVE.md, “Cashflow final reconciliation acceptance”.

The original 6A posting-comparison backend is accepted with 6B; the horizontal screen/export follow in 6C/6D. Evidence: CHANGELOG_ARCHIVE.md, “Horizontal Cashflow accounting bridge”.

Aadil approved the horizontal specification and revised sequence below. Detailed contract: CHANGELOG_ARCHIVE.md, “Horizontal Cashflow reconciliation specification”.

## Cashflow reconciliation sequence — 2026-10-22

Cashflow history and remaining OAuth follow-up; full-database backup work is above. Earlier roadmaps do not authorise renewed performance/prepared-copy work. Preserve canonical `client_data`, complete vouchers and verified archives.

| Phase | Status / objective |
|---|---|
| 1 — Foundation | Complete: account-scoped ledger metadata, roles, source controls and import safeguards. |
| 2 — Ledger upload | Accepted by Aadil after successful physical import. Standard mapping, account/coverage identity, source notes and existing duplicate/archive/receipt safeguards remain. |
| 3 — Client Data | Accepted by Aadil. Existing preview has searchable uploaded-account selection, combined period/row filters and account-scoped counts, pagination and caching. |
| 4 — Balances | Complete and accepted; included in the combined commit/push authorised on 2026-10-21. |
| 5 — Cashflow rows | Complete; combined commit/push authorised on 2026-10-21. Expandable dim-green balances, account/code hover, calculations, split totals and fresh evidence. |
| 6A — Specification | Accepted/committed (`c32e6bb`). Horizontal receipts schedule, then payments, with supported adjustments, separate differences and ledger balance proof. |
| 6B — Accounting bridge | Accepted/committed (`2f9612d`). Independent gross sides, supported presentation adjustments, separate residuals and voucher support preserve accepted Cashflow policy. See archive. |
| 6C — Screen | Accepted/committed (`fdf3e22`, `8975eee`): full-page working paper, loading, validated caches and dates locked to the loaded Cashflow report. |
| 6D — Excel | Accepted/committed (`3b1762b`): streaming workbooks, comparison reuse, bounded exports, progress/cancellation, private files and first-opening repairs preserve existing Cashflow exports. |
| 7 — Reconciliation acceptance | Accepted/committed (`c08ac8e`). Twenty view/range combinations, eight workbooks and screen parity verified; 183 targeted tests pass. |
| 8 — Account selection | 8A–8C accepted/committed (`4f0433d`); derived-column fetching preserved. |
| 8D — Compact reconciliation | UI and group-cache correction accepted and pushed with Phase 10 (`77eb806`). |
| 8E — Reconciliation opening performance | Accepted and committed/pushed (`b0a907b`); actual-source/Excel comparisons and 297 regression tests pass. Main-report path unchanged. |
| 9 — Selected-account acceptance | Accepted by Aadil; reports work correctly. Existing focused acceptance evidence is retained. |
| 10 — Voucher completeness popup | Core and hover fix pushed (`77eb806`, `27aa112`); tab-return check tested and accepted. |
| 11 — Drive production — final | Keep Render Free for reporting; local replacement safeguards and synthetic Drive upload/download/checksum/extraction passed. OAuth publishing status remains unverified. |

Each implementation phase retains file/approach approval, local acceptance and separate Git approval.

**OAuth follow-up:** Check publishing status in Google Cloud Console before treating long-term token readiness as verified.

Agreed toolbar: one desktop row, narrower Group by/Treasury view/Period, Cash/Bank a/c after Period, and shorter Ledger drill-down / XLSX labels. Default All preserves accepted behaviour.

## Active roadmap - Excel-style Cashflows database strategy - 2026-10-08

This roadmap supersedes the earlier future-phase sequence while preserving its completed work. The design target is a simple PostgreSQL/Supabase implementation that behaves like the user's Excel model: immutable raw data, upload-time derived transaction data, and fast prepared summaries instead of repeated full recalculation.


### Prepared-read-model transition checkpoint - 2026-10-09

**Status:** Core transition implemented; acceptance and fallback retirement remain pending.

Completed: prepared-line, rollup, state, and job schema; initial backfill; incremental upload, category, and Cash/Bank preparation; revision-safe active switching; prepared summary/classification/drill-down/export readers; and durable failure/retry recovery.

Remaining: prove the 2-second half-year, 3-second full-year, and sub-500-ms warm targets; complete parallel reconciliation across all report views and exports; repair/retry the historical client-5 failed job through normal application recovery; then retire only proven-obsolete fallback work.

**This phase:** Phase 4 transaction-safety hardening is locally accepted and ready for the next performance/reconciliation gate.
### Phase 0 - Database baseline and design proof
**Status:** Starting; read-only investigation first.

Confirm the current schema, report source, derived-data grain, upload/batch metadata, indexes, snapshot/read-model path, and measured time spent in cold report generation, warm validation, category expansion, and category changes. No schema or application change is allowed until the design is documented and approved.

**Exit condition:** one agreed database design covering raw Client Data, upload batches, winning Voucher IDs, prepared transaction rows, category revision, summary read models, cache invalidation, and required indexes.

### Phase 1 - Business and accounting contract
**Status:** Completed.

The contract remains: one winning occurrence per normalized non-blank Voucher ID; retain every line from the winning upload; preserve raw data; assign categories through Account Control Centre; and use the same valid population across all Cashflows views.

### Phase 2A - Duplication gate
**Status:** Implemented and locally verified; retain as a completed gate.

The shared latest-voucher selector prevents filename-based overlap while retaining legitimate repeated account lines. The parked canonical occurrence register remains optional until Phase 0 proves it is needed for performance.

### Phase 2B - Active canonical Voucher register
**Status:** Implemented and backfilled for client 5/template 1; Aadil acceptance remains pending.

The existing occurrence register is now the preferred report selector, with the fresh-start SQL window retained as fallback. Historical decisions were backfilled from current prepared classifications, preserving raw Client Data and derived classification rows. The register contains one current occurrence per normalized non-blank Voucher ID, while older exact duplicates and superseded versions remain auditable.

**Evidence:** 44 active uploads processed; 32,791 occurrence decisions stored; 13,014 current non-blank winners; zero duplicate current normalized Voucher IDs; 60,222 selected classified lines with no duplicate classification row join. The canonical source hash matches the previous latest-voucher population hash.

**Remaining:** full cold report timing is not accepted here; Phase 6 must measure and optimise the remaining classification/source query. The browser/server cache and accounting totals must be tested by Aadil before Git actions.
### Phase 3 - Upload-time prepared transaction data
**Status:** Pending; follows Phase 0 approval.

Prepare reusable normalized Voucher ID, account, counter-account, category, date/month/period, amount, party, and cashflow-classification fields during upload. Keep raw Client Data immutable and make prepared data rebuildable.

### Phase 4 - Fast Cashflow read models
**Status:** Partially present; optimisation pending.

Use compact indexed summaries for the main report, category expansion, Customer Payments, customer-wise, period-wise, and XLSX outputs. Avoid repeating full derived-window preparation during every report request.

### Phase 5 - Category-change and cache safety
**Status:** Partially implemented; targeted refresh remains pending.

Category changes must update the authoritative mapping, refresh affected prepared data or summaries, advance the report revision, invalidate stale caches, and prevent another Cashflows tab from silently retaining old results. Preserve the existing hard-refresh and revision protections.

### Phase 6 - Performance validation
**Status:** Pending.

Acceptance targets are: full-year cold report approximately 3 seconds maximum; half-year and shorter cold report approximately 2 seconds or below; warm report under 500 ms. Measure category expansion separately for cold and warm use.

### Phase 7 - Accounting reconciliation
**Status:** Partially validated; full matrix pending.

Validate ordinary receipts, payments, revised/backdated uploads, duplicate Voucher IDs, repeated account lines, blank Voucher IDs, category changes, Customer Payments, customer-wise, period-wise, and XLSX results.

### Phase 8 - Cleanup, documentation, and Git
**Status:** Pending.

Decide on obsolete superseded code only after correctness and performance acceptance. Update documentation, wait for Aadil's local-server test, and stage/commit/push only after explicit approval.

# Development Plan

## Cashflows fresh-start duplication gate — 2026-10-08

### Phase 2A — Derived-window duplication gate

**Status:** Implemented and verified locally; Aadil acceptance remains pending.

The derived classification window must use the same Excel-style source population as Cashflows: one latest complete occurrence per normalized Voucher ID, ordered by upload date and then upload ID. Raw Client Data and derived classification history remain immutable; blank Voucher IDs stay visible and separately flagged.

**Exit condition:** the derived preview and Cashflows SQL source return no repeated non-blank normalized Voucher ID from older uploads, preserve every line from the winning occurrence, and retain legitimate repeated account lines. Only after this gate passes will the Phase 2 comparison report be accepted.

### Phase 5A — Warm-cache freshness timing gate

**Status:** Planned; log review shows the existing warm path is not yet within the agreed target.

This gate follows Phase 5 cache-safety work and precedes Phase 6 performance optimization. The existing browser/server cache protection must remain authoritative: freshness checks cannot be removed merely to make the report appear faster. Measure the warm request as separate stages—freshness/revision validation, cached summary reuse, report rendering, and total time—then optimize the slow stage only where the evidence justifies it.

The supplied WebInspector trace recorded a 2,439.7 ms Cashflows preview and two revision checks of 2,304.0 ms and 2,090.4 ms. Server wait accounted for almost all of each request, so the trace indicates an expensive freshness/source-identity check rather than a transfer-size or browser-rendering problem. The trace does not prove that the summary cache itself missed because it contains no repeated identical preview request.

**Acceptance:** retain the cold target of approximately 2 seconds; run five repeated requests for the same complete report context after the first successful load, with every warm report generation under 500 ms end-to-end. Record the server revision-check and cached-summary stages separately. If the warm target fails, optimize the freshness lookup or request sequencing without weakening revision/source-key invalidation, stale-response protection, hard refresh, or accounting correctness.

## Adjusted priority - Verification workstreams - 2026-09-24

Financial Statement preview groundwork is committed for future use, but Financial Statements and Notes remain deferred operationally. The active scope is verification of Cash, Bank, Local Vendor, Import Vendor, Export Customer, Payroll, and Contractor vouchers.

| Workstream | Current position | Remaining action |
|---|---|---|
| Cash / Bank | Implemented and live; Account Control routing, legacy fallback, Treasury handling, and debit/credit presentation are covered. | Final live-client acceptance only. |
| Local / Import Vendor | Implemented and live; central account mapping, JV discovery, Treasury exclusion, Preview, and sessions are covered. | Confirm published/account-control mappings and representative periods on the client. |
| Export Customer / Payroll / Contractor | Implemented and live in Verification with discovery, Preview, sessions, and workstream-specific movement rules. | Change the global quick-navigation entries from Coming soon to Ready, then complete live-client acceptance. |
| Cross-category Treasury boundary | Intentional: cash/bank payment and receipt legs remain in Cash/Bank; billing/JV legs remain in the relevant workstream. | No change required. |

Next verification phase: correct the three global quick-navigation labels, then run one acceptance pass per workstream using real Account Control mappings and date ranges. Do not extend or operationalize Financial Statements or Notes until this verification scope is accepted.

## Current roadmap - Rulebook snapshots and Verification lookup - 2026-09-22

Recommended model: GPT-6 Astra; shared/client separation, concurrent publication, migrations, and historical-result protection require careful cross-file reasoning.

Step 4 investigation and the compiled-object contract are approved. See CHANGELOG_ARCHIVE.md, "Rulebook architecture decision and snapshot foundation".
Extend the existing client/session Rulebook design: shared Category snapshots plus one global block; client-specific resolution stays outside shared storage.
Assertions remain coverage metadata. Content revisions and evaluation fingerprints serve different purposes.

| Step | Status / next action |
|---|---|
| 4 - Architecture decision | Complete; synthetic database-call baseline recorded in the archive, not a production latency claim. |
| Postgres migration fix | Complete, committed and pushed (`317337c`); existing-PostgreSQL hash-column migration wired into the fast path, test-covered, user-confirmed live. |
| 6 - Session protection | Complete: existing mismatch/rebase protection remains; rebase updates only the session version, never saved results or hashes. |
| 6 - Source consistency | Active Verification blocks all Audit Rules writes; the separate multi-commit authoring boundary remains recorded for future hardening. |
| 6 - Saved results | Complete: unchanged hashes are current; changed hashes need recheck; legacy null hashes are unknown; no load/rebase recalculation. |
| This phase | Scoped Category snapshots remain usable after a global-only rebuild. Phase 4 batching was rejected and removed after variable 7-21s server waits; remote Supabase/service latency remains the practical limit. Final normal-save confirmation and separate commit/push approval remain. |
| 7 - Release validation | Next: prove unchanged-rule equivalence, measure against Step 4, run regression/browser acceptance, then obtain separate commit/push approval. |

Status-log compression remains separate and requires the requested "generate brief" confirmation; File_Map.md needs review before new rows.
The older roadmaps and closeout sections below are historical context, not authorization to reopen their phases.

## Previous roadmap — Global Loading Feedback and Interaction Locking — 2026-09-13

Recommended model: GPT-5. This cross-app UX change spans shared loading behavior, navigation, asynchronous reads, and high-risk workflow actions.

### Phase 0 — Interaction policy and acceptance rules

Status: completed through discussion.

Use the standard `progress` cursor as an ambient signal while retaining local spinners, skeletons, and processing banners. Lock the initiating control or affected workflow only; unrelated actions remain available where safe. Release loading on actual completion, error, cancellation, or interrupted navigation—never through a guessed timeout. Overlapping work must use reference counting or tokens.

### Phase 1 — Shared foundation and one pilot

Status: implemented and accepted; dashboard pilot released in commit `6905ad5`.

Add the reusable global cursor behavior through the existing loading infrastructure and prove it on one representative low-risk flow. Verify immediate feedback, duplicate-click protection, safe unrelated interaction, and recovery on success, failure, cancellation, and interrupted navigation. No backend changes are expected.

### Phase 2A — Procedure-modal loading feedback

Status: implemented and accepted; released in commit `bc67341`.

Added target-aware global `progress` cursor ownership to the shared Loading UI and the reusable
Procedure Modal. Existing local spinners, modal cleanup, and unrelated interaction remain intact.

### Phase 2B — Navigation and remaining modal-opening rollout

Status: implemented and accepted; navigation code and shared-script cache busting completed.

Extend the behavior to dashboard/module cards, header and quick navigation, async dropdowns, modal/detail openings, and template/supporting-document views. Handle the transition between the old and newly loaded page, including Back/bfcache and browser Stop behavior.

### Phase 3 — High-risk workflow actions

Status: Phase 3A, Phase 3B, and Phase 3C implemented and accepted; Phase 4 candidate files require confirmation.

Apply narrow locking and progress feedback to Save, Verify, Complete, Import, Delete/Restore, Verification Prev/Next, Audit Rules writes, and other actions that can conflict with the same data or workflow. Preserve independent application use where it cannot interfere.

### Phase 4 — Coverage and hardening

Status: Phase 4A, Phase 4B, and Phase 4C implemented and accepted; hardening Mini-phases 1–5 are complete. The standard `progress` cursor is retained; custom animation remains optional.

Hardening audits covered slow/failed/parallel requests, stale cleanup, session expiry, keyboard/touch
use, reduced-motion accessibility, and stuck-cursor or stuck-control recovery through the shared
utilities. Broader per-page coverage, long-running determinate work, and a custom animated cursor
remain optional and deferred.

Each phase requires candidate-file confirmation, approach approval, implementation, proportional verification, and Aadil’s test confirmation before the next phase. After every completed phase, provide a plain-language walkthrough of the changes made and the remaining test points. No staging, commit, or push occurs before explicit approval.

## Current roadmap — Ledger Reconciliation & Monthly Category Summary — 2026-09-10

Recommended model: GPT-5. The feature combines financial calculations, existing client data, configurable account groups, Excel output, and reconciliation controls.

The uploaded `Bank_Monthly_Summary v1.xlsx`, `Cash_Monthly_Summary.xlsx`, and `1. All Vouchers (Jan To June) - Copy.xlsx` are the golden reference set. The full journal is the source of truth; legacy Bank/Cash working tabs must not drive calculations.

### Phase 0 — Final specification and golden baseline

Lock the source contract, account-code-only group membership, category source, rounding/tolerance policy, opening/month-end balance input method, reference tab structures, and report blocking rules. No implementation expansion occurs until these decisions are clear.

### Phase 1 — Core ledger summary engine

Status: implemented and accepted.

Files: `services/ledger_group_summary.py`, `tests/test_ledger_group_summary.py`.

The pure pandas engine selects complete vouchers by explicit account code, separates group/contra legs, stops on imbalance, normalizes categories, reports unmapped accounts, identifies internal/related-group transfers, builds monthly pivots, and optionally derives combined/per-account reconciliation proofs. Phase tests and the full regression suite pass; the engine replay matched Bank/Cash reference movements and totals.

### Phase 2 — Existing-app data adapter

Status: implemented and accepted.

Files: `services/ledger_report_source.py`, `tests/test_ledger_report_source.py`, and documentation updates.

Read the client’s complete journal from stored `client_data`, resolve the normalized voucher fields, read existing account-category assignments, reject incompatible source layouts clearly, and never filter by account name or voucher prefix.

Acceptance: the adapter produces the same normalized DataFrames used by Phase 1 against the uploaded Jan–Jun source structure.

### Phase 3 — Configurable account-group backend

Status: implemented and accepted (backend-only; automated verification).

Files: `database.py`, `routers/reconciliation.py`, `main.py`, `tests/test_reconciliation.py`, and documentation updates.

Add client-scoped `account_groups` storage for group label and exact account code. Provide suggestions from account type without trusting them, support silent accounts, prevent duplicates, and allow Bank, Cash, and future groups without code changes. SQLite and PostgreSQL schema paths must remain aligned.

Acceptance: Bank/Cash membership can be edited and reused across periods; names cannot affect membership.

### Phase 4 — Account-group configuration interface

Status: implemented and accepted (frontend-only; automated and browser smoke verification).

Files: `static/reconciliation.html`, `static/reconciliation.js`, `static/style.css`, `tests/test_reconciliation.py`, and documentation updates.

Expected scope: `static/reconciliation.html`, `static/reconciliation.js`, `static/style.css`.

Provide group selection, searchable account-code membership, account title/type, transaction counts, include/exclude controls, warnings for unusual classifications, save feedback, and a visible selected-code summary.

Acceptance: an auditor can review and save Bank/Cash codes without editing code.

### Required design decision — report category dimension

The existing Trial Balance `category_id` and `account_category_assignments` belong exclusively to Audit Verification's `audit_rule_categories`; they must not feed Ledger Summary or future Financial Statements.

Recommendation: keep Trial Balance as the central account master, but add report-scoped category sets and account-code assignments. The first set is Ledger Summary; a later set is Financial Statements. Each set owns its category definitions and assignments while sharing the same account codes. Do not add another meaning-bearing category column to `trial_balance_accounts`.

Decision status: confirmed and implemented in Phase 4A. This category foundation must remain separate from Audit Verification and is required before Excel export and report UI work.

### Phase 4A — Report category dimension foundation

Status: implemented and accepted (backend-only; automated verification).

Files: `database.py`, `routers/reconciliation.py`, `services/ledger_report_source.py`, `tests/test_reconciliation.py`, `tests/test_ledger_report_source.py`, `tests/test_pg_pool_retry.py`, and documentation updates.

Add aligned SQLite/PostgreSQL report category sets, definitions, and account-code assignments. Provide admin APIs for set/category/mapping replacement, validate codes against the latest parsed Trial Balance, and make Ledger Summary read only the `ledger_summary` set. Audit Verification tables remain untouched.

Acceptance: the same account code can have independent Audit Verification, Ledger Summary, and future Financial Statements classifications; missing Ledger Summary mapping is visible as a review warning rather than silently inherited.

### Phase 5 — Excel export engine

Status: implemented and ready for user acceptance (backend/export-only; automated verification).

Expected scope: `services/reconciliation_xlsx.py`, exporter tests, and confirmed documentation.

Generate the golden Bank and Cash workbook structures through `services/reconciliation_xlsx.py`, including monthly summaries, transfer schedules, cross-checks, review schedules, combined proofs, and per-account proofs. Apply Arial, dark-blue headers, light-blue totals, green matched cells, orange variance cells, freeze panes, safe number formats, chronological months, and safe text-cell handling. Missing balance inputs remain unavailable rather than zero.

Acceptance: focused exporter tests pass; generated Bank/Cash movements match the supplied reference workbooks to floating-point tolerance; no non-zero category is silently omitted. No Reports API, dashboard navigation, or balance-upload UI is included in this phase.

### Phase 6 — Opening balances and reconciliation-proof input

Status: implemented and ready for user acceptance (backend-only; automated verification).

Implemented the recommended standardized `report_balance_inputs` table containing client, group, account code, reporting month, opening balance, and actual closing balance. Added aligned SQLite/PostgreSQL schema and indexes, transactional replacement/list APIs, exact Trial Balance and saved-group membership validation, and `services/reconciliation_balance_source.py` for completeness diagnostics and engine-map conversion. No upload workflow or UI is included yet.

Acceptance: 17 focused Phase 6 tests pass; combined and per-account proofs can consume normalized inputs; missing accounts/months, duplicates, invalid values, and inconsistent openings are reported; zero balances remain valid. Report API/UI integration remains Phase 7 scope.

### Phase 7 — Reports API and Reports screen

Status: implemented and accepted (backend/UI integration; automated and manual verification). Existing PostgreSQL startup now creates account groups and indexes, correcting live Reports input loading on mature databases.

Expected scope: `routers/reconciliation.py`, `main.py`, `static/reconciliation.html`, `static/reconciliation.js`, dashboard files, `static/client_context.js`, and styling as confirmed.

Replaced the Reports placeholder with selected-client Client Data Bank dataset, period, and group selection; source diagnostics; account-group review access; report preview; visible control statuses; and guarded Excel download. Account setup uses only the selected session client, never a manual or URL Client ID. Registered the module in `NAV_MANIFEST` and wired the dashboard Reports card using existing navigation conventions. The existing summary, balance-source, and Excel-export engines remain unchanged.

The UI must show ledger balance, review-category count, transfer status, cross-group status, and reconciliation status. Final download must be blocked when the accounting controls fail.

Acceptance evidence: 17 focused Phase 7 tests and the full suite (224 tests, 33 subtests) pass; JavaScript/Python syntax checks pass. The existing-PostgreSQL migration is bootstrap-tested; manual acceptance confirmed selected-client Trial Balance heads load in Account setup. `main.py` required no change because the reconciliation router was already mounted.

### Phase 8 — Selected-client report configuration UI

Status: implemented and ready for user acceptance (frontend integration; existing APIs/storage retained).

Added client-context Account setup sections for Ledger Summary categories and reconciliation balances. The category section creates the client’s dedicated `ledger_summary` set, defines optional hierarchy, and maps exact Trial Balance codes without touching Audit Verification categories. The balance section loads saved groups, records first opening and each actual closing, carries later openings forward, and preserves valid saved months outside the edited range.

Acceptance: configure a real Ledger Summary set, assign Trial Balance accounts, enter Bank/Cash balances, then preview a report. Existing category/balance API tests and JavaScript syntax checks pass; manual acceptance remains required.

### Phase 9 — Golden acceptance and hardening

Run Bank/Cash golden comparisons plus tests for account-name variation, unmapped accounts, internal transfers, cross-group transfers, missing balances, non-zero variance, unbalanced vouchers, silent accounts, case/whitespace category variants, multiple periods/clients, and large journals. Complete documentation and record only genuine deferred findings.

### Phase gates

Each phase requires: candidate-file confirmation, approach approval, implementation and proportional verification, then Aadil’s test confirmation before the next phase. No staging, commit, or push occurs before explicit approval.

Expected file lists are planning candidates only; files for each phase must be reconfirmed before implementation. Phases 1–3 and 4A are implemented; user acceptance remains required for 4A before Git closeout.

## Previous final combined closeout - 2026-09-09

Aadil requested one final phase covering acceptance, safety, release checks, and Git closeout.
The dashboard correction was manually accepted. The Ledger Reconciliation roadmap above is a new brief that supersedes the earlier “no further phase” statement.

This section records the prior closeout. Earlier phase tables and proposed next actions are
historical; they must not reopen completed work. See `CHANGELOG_ARCHIVE.md`,
"Final combined closeout - 2026-09-09". The previous roadmap remains available in Git at
`e6bfe11:DEVELOPMENT_PLAN.md`.

| Scope | Final development status | Evidence / limit |
|---|---|---|
| Form A-I | Implemented and accepted | Rendering, Copy from lines, typed values, blanks, migration, and legacy retirement completed. |
| Governance D1-D9 | Implemented and accepted | Canonical Category Documents, dependency validation, frozen Rulebook, migration safeguards, and inactive configuration handling completed. |
| P1 / P7 | Accepted; desktop checks passed | Verification and Workbook counts accepted; Category tabs and shared modal checked. Whole-page mobile width remains a recorded limitation. |
| P2-P6 / Safety A-B | Combined regression completed | 183 tests and 33 subtests pass; includes isolated presence, status gates, Forms, Cross-checks, save ordering, and recovery. Live reads supplement prior user acceptance. |
| P8 | Review completed | Existing measurements retained; further tuning requires a measured new issue. |
| P9 | Release review closed by user | Health, historical logs, manual startup, and fixture isolation checked; full PostgreSQL backup/restore evidence remains an accepted limitation. |
| Dashboard correction | User-tested, pushed, deployed | Render returns 33.9 MB / 87,943 active rows; PostgreSQL health ready. Implementation commit: `5373c72`. |

## Release assessment

| Phase No | Phase/Pendency | Objective | Status |
|---|---|---|---|
| This phase | Final closeout | Preserve the recovery limitation for future operations review. | Closed by user; no further development phase is scheduled. |

Export Backup covers templates/imported data/settings, not the full audit database. Old SQLite
backups do not prove recovery of current PostgreSQL data. No full organizational-data export or
destructive restore was performed; the user accepted this limitation to close the plan.

Local startup is manual through the tray launcher; no matching automatic startup task or
Startup-folder entry was found. Running processes retain old Python code until restarted.
Browser checks used fixture client context and suppressed writes; they do not certify simultaneous
authenticated production editing or every live write path.

## Retained data and future work

Form 44 remains active with 86 migrated entries; inactive Vendor Bill 19 retains 43 source bills
across 42 lines. These are business records, not cleanup targets. This closeout created no live
test records; temporary test databases were cleaned up.

Unrelated historical findings and optional enhancements remain in `KNOWN_ISSUES.md`; the Rulebook roadmap at the top governs the current brief.
