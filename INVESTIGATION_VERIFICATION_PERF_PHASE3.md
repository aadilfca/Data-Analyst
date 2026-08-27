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
