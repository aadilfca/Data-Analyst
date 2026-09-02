# Verification Speed Optimization — Phase 3

**Type:** Investigation + fix log for Phase 3 (procedure-modal load ~7.9s, Verify sequence ~21.7s).
Reconciles with, and extends, `PERFORMANCE_INVESTIGATION.md` (2026-08-24 / 08-25) — it is not
re-diagnosed here.

Phase map: 3a reconcile + map (done) · 3b folded into 3c as a baseline step · 3c modal-open
fetch fix (this) · 3d Verify-sequence breakdown · 3e Verify-sequence fix · 3f Render test + docs.

---

## Phase 3a — Reconciliation with PERFORMANCE_INVESTIGATION.md

Both prior passes stand. Phase 3's two slow paths are **specific instances of Finding #1
(fixed cross-region round-trip tax, Render Singapore ↔ Supabase Mumbai) multiplied by
Finding #2 (sequential, non-batched round-trips)** — no new root-cause class.

One earlier statement needed updating: Finding #2 credited the 2026-08-25 `Promise.all` fix
with removing Verification's stacked frontend GETs. True for **page/session load**, but NOT
for `openProcedureModal()` or the Verify sequence — both were built later (Fast Modal-Open
Data, 2026-08-26) and still ran their steps sequentially.

### Modal-open call pattern (`openProcedureModal()`, static/procedure_modal.js)

| # | Call | Notes (pre-3c) |
|---|---|---|
| 1 | `fetchAuditGuidanceData(type)` → `GET /api/verification/audit_guidance` | Usually a cache hit (`auditGuidanceCache`) — 0 network common case |
| 2 | `Promise.all(templateIds.map(fetchTemplateColumns))` → `GET /api/templates/{id}` ×N | Parallel within step; cached across opens. Cold once per template |
| 3 | `apiFetch('/api/verification/procedure_modal_data/{uid}')` | The batch endpoint |
| 4 | `fetchLatestTbAccounts()` → `GET /api/trial-balance/latest-accounts` | **Not cached across opens** (no cache-hit guard); only the Account-Head tab uses it |

Steps 2, 3, 4 are independent of each other but were `await`ed one after another → 3 stacked
browser→server round trips at the ~1–3 s region floor.

**Duplication inside the "batch" endpoint.** `get_procedure_modal_data()`
(routers/verification.py) is a *thin sequential fan-out* to 5 read sub-functions
(`get_voucher_line_documents`, `get_procedure_checks`, `get_line_observations`,
`get_voucher_line_physical_bills`, `get_cross_check_results`). Each independently calls
`_resolve_sibling_voucher_line_ids()` (2 cross-region queries), which was **not memoised** —
so one modal-open request ran that same resolve 5× for the same id.

### Verify-sequence preliminary map (full breakdown is Phase 3d)

`performVerifyVoucherChecks()` (procedure_modal.js) runs 4 chained server calls:
1. `delete voucherChecksCache[...]` then `ensureVoucherChecksLoaded()` — **forced** re-fetch of `/voucher_checks`
2. `flushPhysicalBillEntries()` — sequential loop, one `POST /save_physical_bill` per dirty bill
3. `saveLineItems(uid, {force:true})` → `_save_line_items_impl` → `sync_line_observations_to_voucher()` (per sibling) + `compute_and_persist_cleared_status()` (~15+ sequential queries; per-document auth-role N+1 in `audit_rules.py get_required_documents_with_authority()`, tracked separately in KNOWN_ISSUES.md)
4. `POST /verify_voucher_checks` → `_verify_voucher_checks_impl` → `_fetch_session_details_grouped` + active-checks + status-rows + `sync_line_observations_to_voucher()` in a loop over every line + `_compute_voucher_nav_result`

Then on **"Verify & Next Line"**, `verifyAndAdvanceVoucher()` runs a **full `openProcedureModal()`
again** (another ~7.9 s) + `updateProgressDisplay()` (2 GETs). Consistent with the 21.7 s figure.

### Auth-role N+1 — NOT touched by Phase 3c

`audit_rules.py get_required_documents_with_authority()` is reached only from
`compute_and_persist_cleared_status()` in the save/Verify path, never from
`get_procedure_modal_data()`'s fan-out. It stays tracked separately in KNOWN_ISSUES.md
(2026-08-25) and is out of 3c's scope.

---

## Phase 3c — modal-open fetch fix

### What changed

| Item | Change |
|---|---|
| Frontend serial awaits | `openProcedureModal()` now starts the `procedure_modal_data` fetch **before** awaiting guidance, then awaits template-columns + modal-data together via `Promise.all`. ~2 stacked round trips removed. |
| Redundant server queries | `_resolve_sibling_voucher_line_ids()` gains an **opt-in per-request memo** (`_sibling_ids_request_memo` `ContextVar`). `get_procedure_modal_data()` sets a fresh dict for its fan-out and resets it in `finally`. Every other caller is unchanged (memo defaults to `None`). Per-request, not TTL: the sibling mapping changes on new-session creation, which can't happen mid-request. |
| `fetchLatestTbAccounts()` | Now cached per client + in-flight-deduped, and warmed **lazily** (fire-and-forget from `renderAccountHeadCard()`) only when the user opens the "Voucher Line" tab — 0 cost on the common modal open. |

### Baseline vs after — `get_procedure_modal_data()` only (in-process, detail_id 193952, real Postgres)

| | DB calls | Wall-clock (3-run range) |
|---|---|---|
| **Baseline (pre-3c)** | 17 | ~4.2 – 4.6 s |
| **After 3c** | 11 | ~2.7 – 3.1 s |
| **Delta** | **−6 calls** | **~−1.6 s (−36%)** |

(Measured via `scratchpad/bench_modal_data.py` — in-process call wrapped in `start_db_metrics()`.
Server-side only; the frontend serial-await removal saves further wall-clock on top, confirmed
in the live UI check — see CHANGELOG_ARCHIVE.md "Verification Speed Optimization Phase 3".)

### Tests

`pytest tests/ -q` → 20 passed, 2 failed. The 2 failures are the known pre-existing
`test_app_smoke` / `test_upload_validation_counts` SQLite-vs-Postgres ones (KNOWN_ISSUES 2026-08-27),
unrelated to this change.

### Live UI verification (Playwright, dev server, session #749 / voucher CPV-0826-442 / detail_id 193770)

Ran after the fix (no "before" browser capture — code was already applied; the parallelism is
proven structurally from resource-timing start/end, and the backend before/after is the
17→11 number above).

| Check | Result |
|---|---|
| Modal opens, rail renders | ✅ `display:flex`, 6 rail items (1 Voucher Line + 5 documents), no console errors |
| `procedure_modal_data` runs in parallel with `templates/*` (was serial pre-3c) | ✅ `procedure_modal_data` starts @23706 ms, the 4 `templates/*` calls start @23711–23714 ms — all in flight together; the batch call no longer waits for template-columns |
| Warm-open component breakdown (`?perf=1`) | `guidance_ms: 0` (cached), `modal_data_ms: ~3263`, `template_columns_ms: ~3263` — the two now resolve **together**, total ≈ max not sum |
| Cold open total | ~7.8–8.9 s (guidance + `max`(templates ~5 s, modal_data ~7.8 s)); pre-3c this was guidance + templates + modal_data + TB-accounts **in series** (~13 s) |
| `/api/trial-balance/latest-accounts` on modal open | ✅ **0 calls** across 2 opens (was 1 per open pre-3c) |
| …after clicking the "Voucher Line" tab | ✅ exactly 1 call; account-head search then returns rows (typed "purchase" → 13 matches) |
| …on a 3rd open + Voucher Line tab again | ✅ still 1 total — per-client cache guard holds, no refetch |

Net: a warm modal open drops ~3.8 s → ~3.3 s (the removed TB-accounts round trip); a cold open
drops ~13 s → ~8 s (templates now overlap the batch call instead of stacking). The residual is
the single unavoidable `procedure_modal_data` cross-region round trip — further gains there are
Phase 3e/infrastructure territory, not 3c.

---

## Phase 3d — Verify-sequence deep map + physical_bills duplication (investigation only, no code changes)

Confirms and deepens the 3a preliminary map. Method: full read of `_save_line_items_impl`,
`_verify_voucher_checks_impl`, `compute_and_persist_cleared_status()`,
`sync_line_observations_to_voucher()`, `_fetch_session_details_grouped()`,
`_compute_voucher_nav_result()` / `_build_verification_line()`, `get_voucher_line_physical_bills()`,
plus the `procedure_modal.js` / `verification.js` call sites. No live Verify click was run —
that mutates real verification state (status, cleared_status, voucher_check_status, observations)
for Leather Crop, and this phase is investigation-only.

### Correction to the 3a note above

The 3a line "per-document auth-role N+1 in `audit_rules.py get_required_documents_with_authority()`,
tracked separately in KNOWN_ISSUES.md" is **partly stale**:

- The **save-path** call — `compute_and_persist_cleared_status()` → `check_approved_by_authority_gate()`
  → `get_required_documents_with_authority()` — is **already batched** (one `SELECT` for the docs +
  `get_document_auth_role_authorities_batch()` + one `IN`-based `signature_specimens` query,
  ~3 round trips total). This was done 2026-08-26 ("batched auth-role IN queries, Phase B").
- The only remaining per-role loop is `has_matching_active_specimen()` inside the
  **`if detail:`** block (lines ~732–737) — reached solely by `GET /line_status_reason/{id}`
  (the "why is this Query?" popover), **not** by the save/Verify path.
- It is **NOT in KNOWN_ISSUES.md** — the reference there was to `PERFORMANCE_INVESTIGATION.md`'s
  2026-08-25 table row ("N+1 per-document auth-role fetch … optional fix"), which predates the
  Phase B batching and now applies only to the detail-mode popover.

**Answer to the brief's yes/no:** the auth-role loop is **not entangled** with the Verify
sequence and 3d/3e should not touch it. (If anything, `PERFORMANCE_INVESTIGATION.md`'s table
row should be marked "save-path portion fixed 2026-08-26; detail-mode remainder is low-value".)

### "Verify & Next" full sequence — one press, crossing into the next voucher

Each numbered step is `await`ed before the next. Cross-region DB round trips estimated from the
code (every `execute_query_pg` / `execute_write_pg` = one ~250 ms Singapore↔Mumbai hop);
`log_audit` is local SQLite and free.

| # | Step (JS) | Endpoint | Server-side DB round trips |
|---|---|---|---|
| 1 | `delete voucherChecksCache[id]` → `ensureVoucherChecksLoaded()` | `GET /voucher_checks` (**forced**, cache deliberately busted) | ~2–3 |
| 2 | `flushPhysicalBillEntries()` | `POST /save_physical_bill` ×N, sequential `for` loop | 0–N (only dirty bills) |
| 3 | `saveLineItems(uid,{force:true})` | `POST /save_line_items` | **~28–32** — see breakdown below |
| 4 | `verify_voucher_checks` | `POST /verify_voucher_checks` | **~10 + 7·M + 2·K** (M = lines in current voucher, K = lines in next voucher) |
| 5 | `openProcedureModal(nextLine)` *(only if an unverified line remains in the same voucher)* | `GET /procedure_modal_data` (post-3c: parallel, guidance+templates cached) | 1 browser hop (~11 server queries) |
| 6 | `updateProgressDisplay()` | `GET /session/{id}/voucher_stats` | 1 browser hop (session-wide rollup) |
| 7 | `hideProcedureModalOnly()` (teardown, when the sequence leaves the voucher) | `updateProceduresCellForLine()` + `updateObservationsCellForLine()` | **3–4 browser hops** — see "duplication" below |

**Step 3 (`save_line_items`) breakdown** — `_save_line_items_impl`:
`detail` status (1) · `_resolve_sibling_voucher_line_ids` (2, **not memoised in the save path** —
the Phase 3c ContextVar memo is opt-in and only `get_procedure_modal_data` sets it) ·
`UPDATE verification_details` (1) · `sync_line_observations_to_voucher(sibling_ids=…)` (~5:
all_entries, line_row, check_rows, `UPDATE observations`, batched entry `UPDATE`) ·
`compute_and_persist_cleared_status(sibling_ids=…)` (~18–22):
 line join (1) · frozen-category `matrix_version` (1) · `get_category_documents` (0–1, TTL cache) ·
 `linked_rows` (1) · `template_rows` (1) · `get_document_auth_roles_batch` (1) ·
 **`get_voucher_line_physical_bills(voucher_line_id)`** — the "Material Purchased" category has a
 `physical_bill` document ("Vendor Bill", id 19), so this fires: internally
 `_resolve_sibling_voucher_line_ids` **(2, redundant — already resolved in step 3's parent)** +
 `header_rows` (1) + `_fetch_physical_bill_items` (1) = **4** ·
 `active_checks` (1) · `ticked_rows` (1) · `sig_rows` (1) · `evaluate_cross_checks_for_line` (~1–2,
 zero cross-checks configured) · `_write_shortfall_observations` read (~1) ·
 `_has_pending_account_head_note` (1) · `check_approved_by_authority_gate` (~3, batched) ·
 final `UPDATE verification_details` (1).

**Step 4 (`verify_voucher_checks`) breakdown** — `_verify_voucher_checks_impl`:
`session` (1) · `_fetch_session_details_grouped` (1 big query + ~3 `get_template_columns`) ·
`active_checks` (1) · `status_rows` (1) · 2 batched `UPDATE`s (2) ·
**`sync_line_observations_to_voucher(row['id'])` in a `for` loop over every line of the current
voucher — NOT sibling-threaded**, so each iteration re-runs `_resolve_sibling` (2) + the ~5 sync
ops = **7 per line** · `_compute_voucher_nav_result` → `_build_verification_line(d)` for every line
of the **next** voucher = **2 per line** (`get_row_data` + a `change_info` query with a correlated
subquery).

**Totals** for a 1-line current voucher crossing into a 2-line next voucher:
step 3 ≈ 30, step 4 ≈ 10 + 7 + 4 ≈ 21, plus steps 1/5/6/7 ≈ 1 + 1 + 1 + 4 browser hops.
≈ **55–60 mostly-sequential cross-region round trips**. At ~250 ms each + browser latency this
lands squarely on the reported ~21.7 s. A 5-line voucher pushes step 4 alone to ~45 (7·5 + …).

### physical_bills/194144 repeated-call finding — where it comes from

`detail_id 194144` = session **751**, voucher **CPV-0826-442**, category "Material Purchased",
account `20-15-50-0001`. That category includes the `physical_bill` document "Vendor Bill" (id 19).

Any line in a category that has a `physical_bill` document triggers a `GET /physical_bills/{uid}`
from **`updateProceduresCellForLine()`** (verification.js) — `needsPhysicalBills` is true, so its
`Promise.all` includes `fetchPhysicalBillsForLine(uid)`, alongside `fetchAssignedDocumentRecords`
(`GET /voucher_line_documents/{uid}`) and `fetchProcedureChecksForLine` (`GET /procedure_checks/{uid}`).
**None of these three is cached.** `updateProceduresCellForLine()` is called from:

1. `applyCategoryToTypeCell()` / Type auto-fill — once per line on session/voucher render
2. `toggleColumnsExpanded()` — once per line, per toggle
3. **`hideProcedureModalOnly()` (procedure_modal.js) — after *every* modal close / Verify / Verify & Stay**, for the line being left
4. voucher-navigation re-renders (`applyVoucherNavRenderData` path)

So opening the modal on that line, verifying, returning, opening again, toggling columns, etc.
each re-fires the same 3 GETs — 6+ `physical_bills/194144` calls across one working session is
fully explained, with matching duplicate counts on `voucher_line_documents/194144` and
`procedure_checks/194144`. All three re-fetch data the `procedure_modal_data` batch endpoint
(Phase 3c) returned for the same line moments earlier.

**Is the duplication inside the Verify sequence itself, or elsewhere?** — **Both, two distinct
mechanisms:**

- **Frontend, adjacent to the Verify tail:** the repeated `GET /physical_bills/{uid}` is fired by
  `updateProceduresCellForLine()`, invoked from `hideProcedureModalOnly()` (the teardown that
  concludes "Verify & Next"/"Verify & Stay") and from the other render paths above — **not** from
  inside `performVerifyVoucherChecks()`. It is a consequence of the Verify action, not a step of
  it, and it also fires many times outside any Verify (initial render, column toggle).
- **Backend, inside step 3:** `compute_and_persist_cleared_status()` calls
  `get_voucher_line_physical_bills()` server-side on every `save_line_items` for a physical-bill
  category, and that call **re-resolves sibling ids** (2 queries) even though `_save_line_items_impl`
  already resolved them one frame earlier — the save-path equivalent of the redundancy Phase 3c
  fixed for the modal-open fan-out.

Neither is entangled with the auth-role loop.

### Candidate fixes for Phase 3e (not actioned)

- Thread the already-resolved `sibling_ids` into `get_voucher_line_physical_bills()` (and give the
  save path the same opt-in ContextVar memo Phase 3c added), removing the redundant re-resolve in
  step 3 and in the step-4 per-line `sync` loop.
- Sibling-thread the `sync_line_observations_to_voucher()` calls in `_verify_voucher_checks_impl`'s
  per-line loop (currently each re-resolves).
- `updateProceduresCellForLine()` / `updateObservationsCellForLine()`: reuse the
  `procedure_modal_data` payload already in `procedureModalState[uid]` after a modal open instead
  of re-fetching `voucher_line_documents` / `procedure_checks` / `physical_bills` per call; or add
  a short per-line client cache keyed by `uid` + a dirty flag set on save.
- Drop step 1's forced `voucher_checks` re-fetch, or narrow it (the cache was just written by the
  same modal's open).
- On "Verify & Next Line", reuse loaded data for the next line rather than a cold `openProcedureModal`.
- Batch step 3 + step 4 into one endpoint (they always run back-to-back and share the session).

### Live trace not captured

A full browser waterfall of a real "Verify & Next" was **not** run — clicking Verify writes
verification state for a real client (Leather Crop), and 3d is investigation-only. The step map
above is from code plus the DB-call arithmetic. A live before/after belongs in Phase 3f's Render
(staging) test, or run against a throwaway session with explicit go-ahead.

---

## Post-fix re-measurement (local dev server), 2026-09-02

**Trigger:** user asked why the procedure window's data fetch still feels slow, right after the
2026-09-02 cross-check shortfall fix. Read-only: 5 consecutive `GET
/api/verification/procedure_modal_data/296378?perf=1` calls (a real, already-linked voucher line)
against the local dev server — this endpoint has no auth guard, so no session/cookie was needed;
nothing was written.

| Run | `support_data_ms` | `voucher_checks_ms` | `cross_checks_ms` | `db_calls` | `db_ms` |
|---|---|---|---|---|---|
| 1 | 3641.7 | 477.0 | 1070.7 | 4 | 1547.4 |
| 2 | 2356.2 | 464.0 | 969.2 | 6 | 1432.9 |
| 3 | 2216.3 | 455.0 | 513.5 | 4 | 968.1 |
| 4 | 2107.3 | 774.4 | 462.2 | 4 | 1236.4 |
| 5 | 1984.4 | 461.7 | 453.9 | 4 | 915.5 |

**No new root-cause class** — this is the same finding #1/#2 from `PERFORMANCE_INVESTIGATION.md`
(fixed cross-region round-trip tax to Supabase Mumbai, still present even for a request that never
leaves this machine, since only the database is remote) and the same endpoint Phase 3c already cut
17→11 DB calls on. One genuinely new nuance from this pass:

**The gap between `db_ms` (SQL execution time only) and each component's own wall-clock time is
real, and it shrinks run over run** — e.g. run 1's `support_data_ms` (3641.7) is nearly 3x that
run's share of `db_ms`, while run 5's `support_data_ms` (1984.4) is much closer to its share. All
three components trend down across the 5 runs even though `db_calls` stays flat (4, occasionally
6). This is the signature of **connection-acquisition overhead**, not query cost: `get_pg_pool()`
(`database.py`) is created with `PG_POOL_MIN=1`, so only one physical connection is pre-warmed;
every additional connection the pool needs under concurrent load is opened on demand, and each
brand-new physical connection pays its own TCP+TLS+Postgres-auth round trip to Mumbai before any
query can run on it — a cost `db_ms`'s instrumentation (which only wraps the `cur.execute()` call)
never sees. Repeated calls reuse already-open pooled connections and warm toward the lower plateau
seen by run 5.

**Not actioned, diagnostic only** — consistent with every prior pass, the residual cost here is
network/infrastructure (round-trip distance to Supabase Mumbai plus per-connection handshake cost
under a cold pool), not a query-design or missing-index problem. `db_calls` varying between 4 and 6
run-to-run (support_data's own conditional branches — `sibling_rows`/`links`/`checks`/`obs`/`bills`/
`items` each skip with `if ... else []` when nothing exists to join against) is expected, not a bug.
Two paths forward, same as `PERFORMANCE_INVESTIGATION.md` already lays out: raise `PG_POOL_MIN`
above 1 so more connections are pre-warmed before traffic arrives (a small, low-risk config change,
worth its own brief if this is ever worth doing) is the only lever specific to this new finding;
the larger region-distance cost remains an infrastructure decision, not a code fix.
