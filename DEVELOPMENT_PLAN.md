# Development Plan

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

Expected scope: `static/reconciliation.html`, `static/reconciliation.js`, `static/style.css`.

Provide group selection, searchable account-code membership, account title/type, transaction counts, include/exclude controls, warnings for unusual classifications, save feedback, and a visible selected-code summary.

Acceptance: an auditor can review and save Bank/Cash codes without editing code.

### Required design decision — report category dimension (proposed before Phase 5)

The existing Trial Balance `category_id` and `account_category_assignments` belong exclusively to Audit Verification's `audit_rule_categories`; they must not feed Ledger Summary or future Financial Statements.

Recommendation: keep Trial Balance as the central account master, but add report-scoped category sets and account-code assignments. The first set is Ledger Summary; a later set is Financial Statements. Each set owns its category definitions and assignments while sharing the same account codes. Do not add another meaning-bearing category column to `trial_balance_accounts`.

Decision status: awaiting confirmation. This category foundation must be designed before Excel export and report UI work.

### Phase 5 — Excel export engine

Expected scope: `services/reconciliation_xlsx.py`, exporter tests, and confirmed documentation.

Generate the golden Bank and Cash workbook structures, including monthly summaries, transfer schedules, cross-checks, review schedules, combined proofs, and per-account proofs. Apply Arial, dark-blue headers, light-blue totals, green matched cells, orange variance cells, freeze panes, safe number formats, chronological months, and safe text-cell handling.

Acceptance: generated figures match the reference workbooks to the agreed rupee/tolerance policy; no non-zero category is silently omitted.

### Phase 6 — Opening balances and reconciliation-proof input

Choose and implement the balance-input method: recommended first version is a standardized table containing group, account code, opening balance, month, and actual closing balance; upload support can follow if needed.

Acceptance: combined and per-account proofs show derived closing, actual closing, variance, and status; missing inputs are reported, not treated as zero; non-zero variance blocks a report from being marked reconciled.

### Phase 7 — Reports API and Reports screen

Expected scope: `routers/reconciliation.py`, `main.py`, `static/reconciliation.html`, `static/reconciliation.js`, dashboard files, `static/client_context.js`, and styling as confirmed.

Replace the Reports placeholder with client/period/group selection, source diagnostics, account-group review, report preview, visible control statuses, and Excel download. Register the module in `NAV_MANIFEST` and wire the dashboard Reports card using existing navigation conventions.

The UI must show ledger balance, review-category count, transfer status, cross-group status, and reconciliation status. Final download must be blocked when the accounting controls fail.

### Phase 8 — Golden acceptance and hardening

Run Bank/Cash golden comparisons plus tests for account-name variation, unmapped accounts, internal transfers, cross-group transfers, missing balances, non-zero variance, unbalanced vouchers, silent accounts, case/whitespace category variants, multiple periods/clients, and large journals. Complete documentation and record only genuine deferred findings.

### Phase gates

Each phase requires: candidate-file confirmation, approach approval, implementation and proportional verification, then Aadil’s test confirmation before the next phase. No staging, commit, or push occurs before explicit approval.

Expected file lists are planning candidates only; files for each phase must be reconfirmed before implementation. Phases 1 and 2 are implemented; Phase 3 is next.

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
