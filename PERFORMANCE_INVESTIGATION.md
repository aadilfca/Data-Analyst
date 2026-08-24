# Performance Investigation — Render/Supabase Latency Root Causes

**Date:** 2026-08-24
**Type:** Read-only diagnostic (no code changes made in this investigation)
**Trigger:** App feels slow — 2–3s page loads generally, up to 19s+ on some modal
loads — despite Fast Data Handling (13 phases, completed 2026-08-24) already
addressing frontend payload/pagination/caching concerns.

## Summary

The database itself is fast and healthy. The slowness is a **network/infrastructure
problem**, not a query-design problem: every request pays a large, mostly-fixed
round-trip tax before any query logic runs, and several code paths multiply that tax
by firing many *sequential* round-trips instead of one. No schema or query redesign
is indicated.

## Ranked findings

### 1. Region mismatch: Render (Oregon, US West) ↔ Supabase (Mumbai, South Asia) — PRIMARY
**Confidence: High. Evidence: direct measurement.**

`EXPLAIN ANALYZE` against the live Supabase database shows the actual query
execution time for the two main suspect queries (`get_latest_cashbook_date()` /
`get_cashbooks()` patterns in `routers/verification.py`) is **under 10ms**, using a
proper index scan (`idx_client_data_deleted`), against a 10,846-row table. Yet the
same calls measured through the live API consistently took **1,000–3,000ms**, even
fully warm with no cold-start involved (confirmed across the 10:13–10:15 log window:
`stats`, `latest_cashbook_date`, `templates`, `voucher_stats`,
`category-for-account`, `voucher_nav` all landed in a 1.3–3.2s band, every single
call, no exceptions).

A ~100–400x gap between "database execution time" and "what the user experiences,"
present on *every* call regardless of warm/cold state, is the signature of a fixed
per-request network/connection tax — not a query problem. Render has no Mumbai
region; its closest option is Singapore.

### 2. Sequential round-trips multiply the region tax — SECONDARY, explains the worst outliers
**Confidence: High. Evidence: code reading + measured outlier endpoints.**

Two mechanisms stack directly on top of finding #1:

- **Frontend**: pages like Verification fire 4–6 GETs one after another
  (`ApiUtils.apiFetch` calls in `verification.js` are not batched via
  `Promise.all`). If each carries a ~1–3s region-tax floor, sequential stacking
  alone accounts for most of a page's perceived load time.
- **Backend**: `compute_and_persist_cleared_status()`
  (`routers/verification.py`, ~lines 400–720) — the function that runs on every
  line save *and* every "why is this Query?" popover / Validate-panel open — fires
  6–10+ **sequential, non-batched** `execute_query_pg()` calls per voucher line
  (`required_template_ids`, `tick_rows`, `sig_rows`, `active_checks`,
  `ticked_rows`, cross-check evaluation, `auth_docs`), plus a nested loop calling
  `has_matching_active_specimen()` once per role per document — an N+1 pattern
  inside an already-sequential chain. At ~1–3s per round-trip, a multi-line
  voucher's procedure modal plausibly compounds into the 19s figure reported.

This directly explains the two standalone slow endpoints measured:
`start_session` (11.2s) and `preview_cashbook` (4.3s) — both do more
sequential per-item DB work than a simple read.

### 3. Render free-tier cold start — CONFIRMED, but not the sustained cause
**Confidence: High (directly observed). Impact: limited to first request after idle.**

Confirmed via Render logs: first request after 20+ min idle is markedly slower;
by mid-session (10:13–10:15 window) there's no restart marker and no repeat of
the extreme spikes. This explains occasional very-slow *first* loads, but not the
1–3s floor present on *every* warm call — that's finding #1.

### 4. One-off 36.2s spike — likely transient pool contention on cold wake-up
**Confidence: Medium. Not reproduced.**

`GET /api/verification/cashbooks` took 36.2s once, shortly after wake-up in an
otherwise-cold session; the identical query later in the same session took 2.88s,
and the spike didn't recur in the later warm window. Best explanation: connection-
pool acquisition contention while the process is still spinning up / establishing
its first cross-region Postgres connections — not a permanent or reproducible query
cost. Not confirmable from logs alone; would need pool-level metrics/logging to
pin down if it recurs.

### 5. Query cost / missing indexes — RULED OUT
**Confidence: High. Evidence: EXPLAIN ANALYZE.**

Both suspect queries execute in under 10ms via proper index scans on
`idx_client_data_deleted`. Connection pooling code itself
(`database.py`'s `get_pg_pool()`/`execute_query_pg()`/`execute_write_pg()`,
~lines 3703–3823) is also correctly implemented — a lazily-created, reused
`ThreadedConnectionPool`, not a per-request open/close pattern. Neither the
schema nor the query/index design needs work.

## Unrelated bug found during this investigation (not performance)

`compute_overdue_uploads()` (`routers/data.py` line 351, `_count_business_days_elapsed`)
throws `TypeError: '<' not supported between 'datetime.date' and 'datetime.datetime'`
on every dashboard load — reproducible. Flagged here per the Code Health Review
convention; needs its own Fix Mode brief, out of scope for this investigation.

## What each finding would take to fix (not actioned — diagnostic only)

- **Region mismatch (#1)**: an infrastructure decision, not a code change — either
  move the Render service to its closest available region to Supabase (Singapore;
  Render has no Mumbai region), or migrate the Supabase project to a US-West region
  (requires a new project + data migration, no in-place region change). This needs
  its own decision/planning conversation given the migration risk, not a quick fix.
- **Sequential round-trips (#2)**: two independent, moderate-effort fixes —
  (a) parallelize independent frontend GET calls with `Promise.all` on
  Verification's page-load fetches; (b) batch `compute_and_persist_cleared_status()`'s
  sequential queries (e.g. combine the per-role `has_matching_active_specimen()`
  calls into one `IN`-based query). **Caution**: this function is the core
  Cleared/Query status engine, and CLAUDE.md's own Status log notes it has been
  "fixed, silently re-broken, and fixed again" multiple times — any refactor here
  needs the existing pytest suite run before/after and careful phase-by-phase review.
- **Cold start (#3)**: either upgrade off Render's free plan (removes spin-down
  entirely) or add an external keep-alive ping — an operational/cost decision, not
  a code fix.
- **Pool contention spike (#4)**: monitor only for now; if it recurs, add
  connection-pool-level logging/metrics to confirm before doing anything about it.
- **Query/index design (#5)**: no action needed.

## Not yet gathered
- `EXPLAIN ANALYZE` re-test on the larger `client_id=1` table (87,561 rows) — lower
  priority now that #1/#2 are confirmed as the dominant cost, but would confirm the
  query-cost conclusion still holds at the larger scale.
