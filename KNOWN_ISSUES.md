# Known Issues / Tech Debt — Internal Audit App

This is the findings log for the **Code Health Review** protocol defined in `CLAUDE.md` (see its "Code Health Review (recurring)" section for cadence, rotation, and the per-session file budget).

Every finding from a review session is recorded here — never fixed silently, never left only in a chat transcript. Reviews are read-only, so an entry landing here is the *outcome* of a session, not a to-do that was skipped.

**Severity meanings:**
- **Fix** — a real defect; should be scheduled as its own phase.
- **Feature** — works as built, but the behaviour itself is incomplete or worth extending.
- **Defer** — known and accepted for now; recorded so it isn't rediscovered from scratch later.

---

## Log

| Date | Module | Finding | Severity |
|------|--------|---------|----------|
| 2026-08-20 | Global Header Nav | Verification's Cashbook/Client Data Links and Trial Balance's Available Trial Balances view have no addressable URL, so they can't be added to NAV_MANIFEST as real destinations | Feature |
| 2026-08-22 | Upload / Audit Rules / Template Viewer | Same client-context load-order race fixed this date in trial_balance.js/databank.js/verification.js: `window.onload` reads `getSelectedClientId()` before `client_context.js`'s async session fetch resolves. Fix is to `await window.clientContextReady` first — deliberately scoped out of that phase | Fix |
| 2026-08-22 | Upload | `static/upload.js` still falls back to `localStorage.getItem('selectedClientId')` in two places (~line 475 and ~line 1748). Nothing writes that key since session-backed client selection landed, so the branch can only return null or a stale pre-migration value. Equivalent fallback already removed from verification.js | Fix |
| 2026-08-22 | Auth/Nav | `static/client_context.js:82`'s comment on `NAV_USER_LABEL` says "GET /api/auth/current-client returns no user fields today, only client id/name/code" — no longer true, that endpoint now returns `username`. Comment-only fix deliberately deferred: correcting it would mean bumping `client_context.js?v=N` across 16 pages. Fix when `NAV_USER_LABEL` is next touched to render the real username app-wide | Defer |
| 2026-08-22 | Navigation | On `client_settings.html` reached with no session client (Admin cogwheel flow), the navbar pill reads "Client: None selected" while the page heading reads "Voucher prefix configuration for <client>". Cosmetic contradiction, confirmed live; fixing means teaching `renderClientControl()` about a URL-supplied client. Deliberately out of scope of the guard fix | Defer |
| 2026-08-22 | Navigation | `?next=` intent is lost if the session expires mid-flow: `client_select.js`'s `guardSession()` bounces to `login.html`, and `login.js` returns to `client_select.html` without carrying `?next=` forward. Rare, and the outcome is safe (picker, then dashboard). Fix would mean threading the param through the login round-trip | Defer |

---

*Entries are appended, never rewritten. When a finding is fixed, leave its row in place and note the fix in `CLAUDE.md`'s Status log — this file is a record of what was found, not a live task list.*
