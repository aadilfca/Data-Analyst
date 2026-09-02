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

**2026-09-02 addendum:** re-measured on the local dev server (procedure_modal_data,
5 runs) — same finding #1 root cause, plus a new nuance (cold-connection-pool
overhead, `PG_POOL_MIN=1`). See `INVESTIGATION_VERIFICATION_PERF_PHASE3.md`'s
"Post-fix re-measurement" section and `KNOWN_ISSUES.md` (2026-09-02).

---

## Final Speed Investigation — 2026-08-25

**Type:** Read-only diagnostic (no code changes). This is the last speed-focused pass —
region is confirmed correct (Render Singapore ↔ Supabase Mumbai) and the remaining
candidates going in were free-tier cold start / Supabase pooler-pause behavior, not
code. This pass covers ground the first investigation didn't: connection handling,
remaining N+1s, index coverage, and frontend/backend concurrency across files not
previously read in depth.

### Summary
No new region- or query-cost-class issue found. Connection pooling and the bulk
import/change-detection write path are both solid. Two small, genuine (non-free-tier)
inefficiencies were found and are optional future fixes. One finding is **not** a speed
issue but a real correctness bug surfaced by this pass — flagged prominently below since
it directly affects the Dashboard's numbers in production.

### Findings

| Technique | Status | Where | Notes |
|---|---|---|---|
| Dashboard stats/KPI endpoints query SQLite for data now migrated to Postgres | **Genuine gap — correctness bug, not just speed** | `routers/dashboard.py` `get_dashboard_stats()` L17-42, `get_dashboard_kpi_stats()` L65-102 | Both use `execute_query()` (SQLite) against `verification_details`/`verification_sessions`/`client_data`/`uploaded_files`/`verifications` — all of these were cut over to Postgres (`execute_query_pg`) in `import_export.py`/`data.py` (Phase 5) and `verification.py` (Phase 6, 2026-08-23). Confirmed live: every `uploaded_files` write in `import_export.py` goes through `execute_query_pg`, so the SQLite copy `dashboard.py` reads is stale/frozen. Practical effect: Open Queries, Verification Progress %, Total Vouchers, Total Uploads, Total Records, Pending Verifications, and Last Upload on the main Dashboard are almost certainly wrong (stale or zero), not just slow. Unresolved Errors is unaffected (`error_log` was never migrated, still genuinely SQLite). `dashboard.py` was never on the migrated-router list in CLAUDE.md's Architecture section — looks like a simple oversight when the other routers were cut over. Recommend its own Fix Mode brief (swap the affected queries to `execute_query_pg`) — out of scope for this speed-only investigation to fix directly. |
| N+1 per-document auth-role fetch in the Approved-by authority gate | Genuine gap — optional fix | `routers/audit_rules.py` `get_required_documents_with_authority()` L75-97, called from `check_approved_by_authority_gate()` | Loops over a Category's required documents, firing one `get_document_auth_role_authorities()` query per document — runs on **every** `compute_and_persist_cleared_status()` call (i.e. every line save), not only the already-documented detail-mode popover loop from the first investigation. Impact is modest (categories typically have few required documents) but it's a real, easily-collapsed N+1 — the sibling specimen lookup two functions below it was already batched into one `IN`-query for exactly this reason (see its own comment, "this gate runs on every `save_line_items()` call"); this one wasn't. |
| Data Bank "search across all columns" filter is an unindexable JSONB scan | Genuine gap — optional fix, low priority | `routers/data.py` L508-512 (`get_data_preview`), L641-647 (per-upload rows) | `EXISTS (SELECT 1 FROM jsonb_each_text(cd.row_data) WHERE LOWER(value) LIKE LOWER(%s))` is a correlated per-row subquery scanning every key/value of every row's JSONB — no plain B-tree index (`idx_client_data_*`, all confirmed present) can serve a substring `LIKE` across arbitrary JSONB values. Not measured as currently slow (no complaint traced to this specific filter), but structurally it's O(rows × columns) and would need a `pg_trgm` GIN index on `row_data` to scale — worth keeping in mind if search ever gets slow at higher row counts, not urgent today. |
| Connection pool release/error handling | Confirmed fine | `database.py` L3741-3958 (`_PooledConnection`, `get_raw_conn_pg`/`release_raw_conn_pg`, `execute_batch_pg`, `execute_batch_insert_with_ids_pg`) | `_PooledConnection.__exit__` always calls `putconn()` (rolling back first on exception), and every raw-connection call site checked (`save_client_voucher_prefixes_pg`, `import_export.py`'s change-detection block) wraps in try/except-rollback/finally-release. No leaked connections found. |
| Bulk import/change-detection write path | Confirmed fine | `routers/import_export.py` L1590-1701 | Already fully batched via `execute_batch_pg`/`execute_batch_insert_with_ids_pg` for the changed/new/missing row groups — explicitly called out in an existing code comment as a deliberate design, not something this pass needed to fix. No per-row `execute_write_pg` loop found. |
| Databank/Verification page-load fetch sequencing | Confirmed fine | `static/databank.js` L1791-1816; `static/verification.js` (2026-08-25 `Promise.all` fix already in place) | Databank's `window.onload` fires a single `fetchAvailability()` call on load — other fetches (preview, templates, upload-frequency, overdue) are on-demand from user interaction, not a stacked sequential chain. No new sequential-fetch chain found beyond what the first investigation and the 2026-08-25 fix already addressed. |
| Quick pass: `audit_rules.py`, `trial_balance.py`, `clients.py`, `templates.py`, `users.py`, `auth.py` | Confirmed fine (shallow pass) | — | No per-row query loops found in hot (frequently-hit) paths. `templates.py`'s `import_backup()` does sequential multi-table writes but is an infrequent admin-only operation (setup backup/restore), not reviewed deeper given negligible traffic. |
| Postgres connection pool sizing (`PG_POOL_MIN=1`/`PG_POOL_MAX=10`) | Free-tier ceiling — no action | `database.py` L3734-3736 | 10 concurrent connections is a reasonable app-side default; any tighter effective ceiling comes from Supabase's free-tier pooler connection cap, not this code. Not independently verified against Supabase's actual limit — only worth revisiting if real contention is observed (see the first investigation's one-off 36.2s spike, finding #4, still unconfirmed as recurring). |
| Render free-plan cold start | Free-tier ceiling — no action | `render.yaml` L10 (`plan: free`) | Reconfirms finding #3 from the first investigation — idle spin-down is a plan-tier behavior, not fixable in code. |

### For the user to decide (not pre-committed as a phase list)
- Dashboard SQLite/Postgres mismatch (correctness bug) — recommend prioritizing this over the other two, it affects what's shown to every user on login.
- Audit Rules authority-gate N+1 — small, safe, mirrors an existing batching pattern nearby.
- Data Bank JSONB search index — no action needed unless search is later observed to be slow.
