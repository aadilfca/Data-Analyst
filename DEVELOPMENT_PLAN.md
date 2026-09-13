# Development Plan

## Current roadmap — Global Loading Feedback and Interaction Locking — 2026-09-13

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

Status: next phase; candidate files require confirmation.

Extend the behavior to dashboard/module cards, header and quick navigation, async dropdowns, modal/detail openings, and template/supporting-document views. Handle the transition between the old and newly loaded page, including Back/bfcache and browser Stop behavior.

### Phase 3 — High-risk workflow actions

Apply narrow locking and progress feedback to Save, Verify, Complete, Import, Delete/Restore, Verification Prev/Next, Audit Rules writes, and other actions that can conflict with the same data or workflow. Preserve independent application use where it cannot interfere.

### Phase 4 — Coverage and hardening

Audit remaining waiting paths and test slow/failed/parallel requests, stale responses, session expiry, keyboard/touch use, reduced-motion accessibility, long-running work, and stuck-cursor or stuck-control recovery. A custom animated cursor remains optional and deferred until the standard `progress` cursor proves insufficient.

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

Unrelated historical findings and optional enhancements remain in `KNOWN_ISSUES.md`. The roadmap at
the top is now the active plan for the new Ledger Reconciliation brief.
