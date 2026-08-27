# Investigation — Verification module UX & performance

**Date:** 2026-08-26
**Type:** Read-only investigation. No code changed, no database writes.
**Scope:** Verification, Audit Rules, Trial Balance (backend + frontend + their shared files).
**Status:** Findings only — this feeds a separate Feature Mode brief.

---

## How this was investigated

Code reading plus live reproduction in a real headless browser (Playwright) against the running
dev server on port 5000, which is pointed at the remote Supabase instance (`ap-south-1`). That
matters: the latency numbers below are real network round-trips to a remote database, which is
the condition the reported problems actually occur under.

Every write endpoint was intercepted at the browser and answered with a synthetic response, so
**nothing this investigation did reached the database**. Session 734 (client 5, "Leather Crop",
CASH IN HAND, 224 lines) was reused read-only rather than starting a new one — deliberately, since
`start_session` for exactly this client/cashbook is a known hang (`KNOWN_ISSUES.md`, 2026-08-25).

One caveat on the numbers, stated up front: repeated automated runs degraded the shared free-tier
database pool, so absolute timings in the later runs are worse than a real user would typically
see. Where that affects a conclusion, it is flagged. The *structural* findings do not depend on
the timings.

---

## Summary

| # | Finding | Severity |
|---|---------|----------|
| **F1** | Cash Voucher tile: full page reload + a blocking session call means 2.4–7.3s of dead time. Both reported symptoms (needs a second click / no processing UI) are this one cause. | **Real bug — UX** |
| **F2** | Prev/Next: ~3 seconds of completely silent saving before any indicator appears; the buttons are never disabled. | **Real bug — UX** |
| **F3** | Caching is in better shape than the brief assumed — but the client-existence check runs *before* the cache is consulted, so a cache hit still costs a database round-trip. | **Performance** |
| **F4** | Every Audit Rules write wipes every client's category cache, defeating the scoped invalidator built one phase earlier. | **Performance** |
| **A1** | `hideSkeleton()` is never called anywhere in the app. A failed voucher navigation leaves the user's whole table as a permanent shimmering placeholder. | **Real bug — proven live** |
| **A2** | One "Next voucher" click fires roughly `2N+2` save calls for an N-line voucher, sequentially. | **Performance** |
| **A3** | `updateProgressDisplay()` is called twice in a row, literally back to back. | **Real bug — minor** |
| **A4** | The three calls in the save/navigate path bypass `ApiUtils`, which is the very thing that would give them a progress indicator. | **Real bug — blocks the F2 fix** |
| **A5** | 734 verification session rows exist; 5 consecutive ones are identical duplicates of the same cashbook. | **Defer / housekeeping** |
| **A6** | Save, validate and complete all swallow their errors to the console. The user is told nothing. | **Real bug — UX** |
| **A7** | Three statements in `CLAUDE.md` are now factually wrong. | **Doc drift** |

---

## F1 — Cash Voucher tile: dead time, not a lost click

**Covers reported points 1 and 2. They are one root cause, not two.**

### What actually happens

Clicking the Cash Voucher tile does **not** open a popup. It navigates the browser to a different
URL of the same page:

- `static/verification.js:3937` — `#cashVoucherTile`'s handler sets
  `window.location.href = '/static/verification.html?type=cash'`.
- The page fully reloads. `window.onload` (`static/verification.js:4117`) then **waits** on
  `window.clientContextReady` at line 4121 — a real `GET /api/auth/current-client` round trip.
- Only after that does it reach `openSessionPopup()` at line 4165.

So the sequence is: click → full page reload → wait for a network call → popup.

### Measured

The page itself is ready almost instantly; the wait is entirely one call.

| Measurement | Result |
|---|---|
| Page DOM ready after reload | **18–20 ms** |
| `GET /api/auth/current-client` | **2,396 – 7,055 ms** |
| Time from click to popup visible | **2.46 – 7.30 s** |

Time-to-popup tracks the `current-client` duration almost exactly. Five consecutive loads gave
3.91s, 4.54s, 2.46s, 4.50s, 4.52s — so this is **not** a cold-start artifact; it is every time.

### Why it reads as "needs a second click"

Measured at 400 ms after the click, and again at 1.5s in three separate runs:

- the URL has **already** changed to `?type=cash`
- `document.readyState` is **already** `complete`
- the popup is `display: none`
- the hub grid is still `display: block` — **and the tile is still fully clickable**

The user sees a finished-looking page with the tile sitting there, nothing spinning, for several
seconds. Clicking again is the natural response. That second click re-runs
`window.location.href` to the same URL, which **restarts the whole reload and the whole wait**.

Two things worth being precise about:

1. **A second click is never actually required.** In every single-click run the popup did
   eventually appear. The user's mental model ("it needs two clicks") is understandable but the
   second click does not cause the popup — it delays it.
2. **I could not prove the second click makes it measurably worse.** My single-click runs varied
   from 12.7s to 50s under a degraded pool, which swamps the comparison. The structural argument
   (a reload restarts the wait) is sound; the timing evidence for it is not, so the fix brief
   shouldn't lean on a claimed number here.

The wide variance (2.4s on a good run, 7s+ on a bad one) is what makes the problem feel
intermittent — "sometimes needs a second click" is really "sometimes it is fast enough".

### Suggested direction

Two independent improvements, either of which helps; both together fix it properly.

- **Stop reloading the page to open an in-page popup.** The tile already lives on the page that
  owns the popup. Opening it directly, and updating the URL without a navigation, removes the
  reload and the second `current-client` call entirely. The existing `?type=` load path should
  stay for genuine deep links.
- **Show the popup first, then fill it.** The popup's chrome does not need the client id — only
  `loadCashbooks()` does, and that function already has its own field-level loading state
  (`static/verification.js:697`). Opening the popup immediately and letting the cashbook dropdown
  show its existing "Loading cashbooks…" state turns a silent multi-second gap into a normal,
  legible wait.
- Regardless of the above, the tile should give immediate feedback and stop accepting repeat
  clicks while it is working.

The underlying `current-client` latency is a separate, app-wide matter — see "Flagged, out of
scope".

---

## F2 — Prev/Next: three silent seconds before any indicator

**Covers reported point 4.**

The brief said there is no processing UI. More precisely: **there is one, but it covers only the
last and fastest step.** `moveToAdjacentVoucher()` does call `showSkeleton()`
(`static/verification.js:3476`) — but by then most of the wait is already over.

### What a "Next voucher" click actually runs

`goToNextTransaction()` (`static/verification.js:3512`), in order:

1. `saveCurrentTransaction()` → `saveAllVoucherLines()` → one save call **per voucher line, in
   sequence** — *no indicator of any kind*
2. possibly the "not validated" confirm dialog
3. `validateTransaction()` → another save, then a validate call — *no indicator*
4. if it passed, `completeTransaction()` → saves everything again, validates again, completes
5. finally `moveToAdjacentVoucher()` → **this** is where the skeleton appears

### Measured (DOM sampled every 150 ms, writes intercepted with a 1.5s delay)

| Time after click | What the user sees |
|---|---|
| 0 ms | click registered |
| 0 → ~3,000 ms | **nothing changes.** No skeleton, no spinner, no disabled button |
| ~3,000 ms | the "not validated" confirm dialog appears |
| ~3,440 ms | dialog dismissed |
| ~4,940 ms | skeleton finally appears |

`nextDisabled` was `false` at every single sample. The button is never disabled, so it can be
clicked repeatedly during the silent window — and each click re-enters the whole chain.

### Suggested direction

- Move the indicator to the **start** of the click handler, not the end — it should cover the
  save phase, which is the slow part, not just the final fetch.
- Disable Prev/Next for the duration of the operation.
- The confirm dialog appearing *3 seconds after* the click is its own problem: the question
  ("this voucher isn't validated, continue?") does not depend on the save, so asking it first
  would make the whole interaction feel immediate and avoid saving work the user then cancels.
- **This fix is blocked by A4** — see below.

---

## F3 — What is and isn't cached

**Covers reported point 3.** Captured by recording every API call across a real session load, two
voucher navigations, and a modal open.

| Phase | API calls |
|---|---|
| Session view load | 9 |
| First "Next voucher" | 11 |
| Second "Next voucher" | **4** |

**The caching works.** The drop from 11 calls to 4 on the second navigation is the client-side
caches doing their job: `auditGuidanceCache` (`static/verification.js:1683`) and
`categoryForAccountCache` (`static/verification.js:2028`) both stop repeat lookups within a page
session. On the second navigation, `audit_guidance`, `category-for-account`,
`voucher_line_documents`, `procedure_checks` and `line_observations` all disappeared entirely.

### Answering the brief's specific question

**"`category-for-account` was deliberately left uncached — is that still true?"** — **No. It is
now cached on both sides**, and has been since the 2026-08-26 Phases 1–3:

- server-side, per client, via `category_for_account_cache_key()` (`routers/audit_rules.py:62`),
  read at `routers/verification.py:3179`
- client-side, per account code, via `categoryForAccountCache`

**It should stay cached** — the invalidation hooks are real and correctly placed (TB upload and
category assignment both call `invalidate_category_for_account_cache()`). But two problems sit
underneath it:

### F3a — A cache hit still costs a database round-trip

In `get_category_for_account()` (`routers/verification.py:3152`), the order is:

1. line 3175 — query the database to check the client exists
2. line 3179 — *then* check the cache

So every cache hit still pays a full round-trip to Supabase. Given the measured latency on this
deployment, that is most of the cost the cache was built to avoid.

**Direction:** consult the cache before the existence check. A cached entry could only have been
written for a client that already passed that check.

### F3b — Category caches are wiped by unrelated writes

`invalidate_audit_rules_cache()` (`routers/audit_rules.py:53`) does a blanket
`_audit_rules_cache.clear()`. The per-client `category_for_account:*` keys live in **that same
dictionary**. So any of the ~16 Audit Rules write endpoints — editing a Document, reordering a
voucher check — throws away every client's category cache.

The section comment (lines 26–30) explicitly reasons that a full clear is correct "regardless of
how sub-phase 2 keys its own cache entries". That reasoning was written *before* the per-client
category keys were added to the same dictionary, so it no longer holds. The scoped invalidator
built specifically to avoid blanket clearing is defeated by the blanket clear.

**Direction:** either keep the two caches in separate dictionaries, or make the blanket clear skip
the `category_for_account:` prefix. Correctness is not at risk — this only over-clears — so it is
a performance fix, not an urgent one.

---

## A1 — Stuck skeleton: the worst thing found

**Severity: real bug. Reproduced live, with a screenshot.**

`hideSkeleton()` exists and is exported (`static/loading_ui.js:45`, `:267`) but has **zero call
sites anywhere in the codebase**. Every skeleton in the app relies entirely on the caller's real
render overwriting the container afterwards.

That assumption breaks on every early return. In `moveToAdjacentVoucher()`
(`static/verification.js:3473`), the skeleton is shown at line 3476, then the function returns
early — leaving it on screen — on **four** separate paths: a failed response, a boundary result,
a render that produces no lines, and any thrown error. `loadNextTransaction()`
(`static/verification.js:1390`) has the same shape.

**Reproduced:** with `voucher_nav` forced to fail once (simulating exactly the kind of transient
network failure this deployment already produces), 10 seconds later the page showed:

- 3 skeleton placeholder rows, still shimmering
- 0 real voucher rows
- no error message anywhere

The header still read "Voucher CPV-0826-153 · Voucher 1 of 132" while the table below it was
empty. The user's entire working view is gone, replaced by a placeholder that will never resolve,
with no indication anything went wrong and no way back except a manual page refresh.

**Direction:** the failure paths need to restore the previous content or show a real error with a
retry. More broadly, a shared loading utility whose "hide" function is never called anywhere is
worth a deliberate decision — either wire it up or remove it, but the current state means every
skeleton in the app is one early return away from becoming permanent.

---

## A2 — Redundant saves on every Next

For a voucher with N lines, one "Next" click issues roughly **2N + 2** save calls plus 2 validate
calls, all sequentially:

- `saveCurrentTransaction()` → `saveAllVoucherLines()` → N saves (`static/verification.js:3693`)
- `validateTransaction()` → 1 more save (`:3703`)
- `completeTransaction()` → `saveAllVoucherLines()` again → N more (`:3744`), then
  `validateTransaction()` again → 1 more (`:3745`)

**Observed:** a 2-line voucher issued 3 saves + 1 validate on a path that stopped early (validation
returned "Failed", so `completeTransaction()` never ran). On the passing path it would be more.

The in-flight guard `inFlightLineSaves` (`static/verification.js:3571`) does not help here. It
deduplicates *overlapping* calls; these are sequential `await`s, so each finishes before the next
begins and every one is a genuine new request. The guard was built for a different problem
(overlapping triggers) and correctly solves that one.

`voucherLineState` does reset per voucher (`static/verification.js:2394`), so this is bounded by
voucher size, not by how many vouchers have been visited — it is wasteful, not runaway.

**Direction:** the save/validate/complete chain re-does work each layer already did. Establishing
once, at the top of the navigation handler, that everything is saved — and having the lower layers
trust that — would collapse most of these calls. This is also the single biggest contributor to
F2's three silent seconds, so the two are worth fixing together.

---

## A3 — `updateProgressDisplay()` called twice in a row

`static/verification.js:1411-1412`:

```
await updateProgressDisplay();
await updateProgressDisplay();
```

Two identical consecutive calls, each triggering a `voucher_stats` fetch. In the `data.completed`
branch of `loadNextTransaction()`. Almost certainly a copy-paste slip. Low impact (it is on the
"all done" path) but unambiguous.

Separately, the session load was measured issuing `voucher_stats` **twice** from two different
legitimate call sites. Worth a look while in the area, but it is not this same bug.

---

## A4 — The save path bypasses the utility that would fix F2

`static/verification.js` has 5 raw `fetch()` calls that skip `ApiUtils` entirely — and three of
them are exactly the calls in the Prev/Next path:

| Line | Call |
|---|---|
| 3637 | `save_line_items` |
| 3706 | `validate` |
| 3748 | `complete` |
| 1007 | `reconcile_range` |
| 1271 | `start_session` |

This matters beyond tidiness. `apiFetchMutate()` (`static/api_utils.js:125`) takes a
`config.target` and **automatically shows and hides a spinner on that element** for the call's
duration. That is precisely the mechanism F2 needs. Routing these three calls through it is likely
most of the F2 fix.

The app's own bypass guard already flags these in the console — the warning is firing and has
simply not been acted on.

Same pattern elsewhere in scope: `static/audit_rules.js` has **20** raw `fetch()` calls (all real
`/api/` writes) and `static/trial_balance.js` has **10**. `static/procedure_modal.js` has zero —
it is fully wired, and is the model to follow.

---

## A5 — Verification session rows accumulating

A read-only count found **734** rows in `verification_sessions`, with ids 730–734 being five
identical entries for the same client and cashbook (224 lines each).

This is the documented consequence of "reopening a cashbook creates a brand-new session row every
time" — the behaviour that `_resolve_sibling_voucher_line_ids()` exists to work around. It is
working as designed, but the table only grows, and every sibling-id resolution has more rows to
consider over time.

**Direction:** housekeeping, not a defect. Worth a decision on whether old empty/duplicate sessions
should be cleaned up. Flagging rather than proposing — this touches the sibling-resolution design,
which has a long bug history and should not be changed casually.

---

## A6 — Errors in the save/navigate path are silent

`validateTransaction()`, `completeTransaction()` and `doSaveLineItems()` all end with a `catch`
that only calls `console.error()`, with explicit comments saying no message is shown
(`static/verification.js:3734`, `:3760`, `:3681`).

Combined with A1, the realistic failure looks like this to an auditor: they click Next, wait
several seconds with no feedback, and end up staring at an empty shimmering table with their
voucher header still showing the old voucher — and **no indication that anything failed**. They
have no way to know whether their work was saved.

**Direction:** a save failure in particular should be surfaced. The app already has a status-message
mechanism (`showStatus()`) and a toast used elsewhere. Silence is the wrong default for a failed
write in an audit tool.

---

## A7 — `CLAUDE.md` statements that are no longer true

Recorded here rather than edited, since no phase has shipped.

1. **`static/api_utils.js` bullet** claims `ApiUtils` is "Wired into every GET/POST/PUT/DELETE in
   `databank.js`/`script.js`/`audit_rules.js`/`verification.js`/`procedure_modal.js`". Not true:
   `audit_rules.js` has 20 raw `fetch()` calls and `verification.js` has 5 (see A4). Only
   `procedure_modal.js` matches the claim.
2. **Status log, 2026-08-26 "Modal Caching Phase 3"** says `category-for-account` was "left
   uncached". Superseded by that same day's Phases 1–3, which cached it on both sides (see F3).
3. **`static/loading_ui.js` bullet** lists `hideSkeleton` as part of the wired API. It is exported
   but never called anywhere (see A1).

---

## Flagged, out of scope — not investigated further

Per the brief, these are named and stopped at.

- **`GET /api/auth/current-client` takes 2.4–7.1 seconds.** This is the single largest cost in F1
  and it is paid on *every page load in the entire app*, not just Verification. It lives in
  `routers/auth.py` / `static/client_context.js`, both outside this investigation's scope. For
  comparison on the same runs: `/api/verification/stats` took 777 ms and `/api/templates` 2,744 ms
  — so this endpoint is on the slow end even by this deployment's standards. **This is probably
  worth its own investigation and may matter more than everything above.**
- **`current-client` bypasses `ApiUtils`** (the console guard flags it), so it gets no retry.
  Same files, same scope boundary.
- **Client 5's orphaned `account_category_assignments`** — already logged in `KNOWN_ISSUES.md`
  (2026-08-26). It is why this client's lines show as Unassigned, which in turn is why the
  procedure modal could not be opened during capture (it correctly no-ops for an Unassigned line).
  Not re-reported as new.
- **`start_session` hang** — already logged (`KNOWN_ISSUES.md`, 2026-08-25). Encountered again and
  worked around, not investigated.

---

## Suggested order for the fix brief

A view, not a decision — the phasing is yours.

1. **A1 (stuck skeleton)** — a real failure that destroys the user's view, cheap to fix, and
   independent of everything else.
2. **A4 → F2 → A2** — one coherent piece of work. Routing the save path through `ApiUtils` gives
   the indicator, which fixes F2; collapsing the redundant saves shortens what the indicator has
   to cover. A6 fits naturally here too.
3. **F1** — self-contained, and the most visible day-to-day annoyance.
4. **F3a / F3b** — cheap performance corrections, no behaviour change.
5. **A3, A7** — trivial cleanups to fold into whichever phase touches the area.
