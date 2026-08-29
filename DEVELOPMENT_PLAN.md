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

1. Measure `/api/auth/current-client` and `/api/clients` separately.
2. Check for duplicate calls and connection-pool waiting.
3. Compare local results with an office/network run only after the local baseline is recorded.
4. Keep any resulting optimization separate from Verification changes.

### Database investigation result

Read-only PostgreSQL `EXPLAIN ANALYZE` checks confirmed that SQL execution itself is not the main delay:

- `latest_cashbook_date` executed in approximately 31.8 ms, but scanned about 60,000 rows and returned about 19,861 rows to the application.
- `cashbooks` executed in approximately 4.5 ms, but returned about 7,859 rows to the application.
- The templates base query executed in approximately 0.07 ms; its user-visible delay is consistent with repeated per-template mapping requests.

The multi-second browser timings are therefore mainly caused by large result transfer/Python JSON processing for Verification discovery endpoints and repeated remote database round trips for template mappings. Query-plan checks required escalated network access because the configured Supabase database is remote.

## Phase status

| Phase | Scope | Status | Next action |
|---|---|---|---|
| Baseline review | HAR and prior-chat review | Completed | Continue with measured diagnostics |
| Grouped Save/Verify diagnostics | `routers/verification.py`, with existing database metrics | Implemented and pushed | Use component evidence during optimization |
| Login/Client Cards investigation | Read-only request timing | Identified | Measure as a separate track |
| Database query-plan investigation | Read-only Postgres `EXPLAIN ANALYZE` | Completed | Reduce Verification result payloads |
| Verification bottleneck optimization | Save/Verify logical and persistence tracks | Active | Optimize one internal stage at a time |

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

The next mini-phase will focus on the backend logical verification stage while preserving the one-click user actions, save-before-verify ordering, transaction boundary, and cache-update timing. Persistence/save optimization will remain a separate follow-up mini-phase.

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
