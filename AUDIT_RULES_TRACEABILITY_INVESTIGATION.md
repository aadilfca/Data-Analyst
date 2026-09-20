# Audit Rules → Verification Status Traceability Investigation

**Date:** 2026-09-20
**Type:** Read-only logic trace (no code changes made in this investigation)
**Trigger:** Confirm that every requirement configurable against a Category
(Documents, Cross-checks, Approval Required, Signature Specimens, Voucher-level
Checks) is actually linked to, checked by, and reflected in the Cleared/Query status
a voucher line shows in Verification.
**Method:** Code path trace across `routers/audit_rules.py`, `routers/verification.py`,
`static/audit_rules.js`, `static/verification.js`, `static/procedure_modal.js`,
`static/audit_workbook.js`, `database.py`. No live client data was read.

## Summary

The pipe from Audit Rules to the status engine is **mostly connected, but not
faithfully**. Cross-checks and Documents genuinely gate Cleared/Query. Three of the
five rule families do not gate anything, two of them by declared design and one by
accident. The two most serious problems are not missing wiring, though — they are
**disagreements**:

1. A Category's per-Document **Required** checkbox is authored, stored, versioned and
   compiled into the Rulebook, and then ignored: the engine blocks on *every*
   attached Document. The Category screen that summarises "what Cleared requires"
   filters on the same flag, so it under-reports the real gate.
2. The Supporting Documents modal decides a Document is done using a **different
   rule** from the one the engine uses for the same Document. For a no-template
   Document the engine wants every authorisation role ticked; the modal renders only
   the roles that the Category's Procedure/Evidence checks resolve to — sometimes
   none at all, leaving a Document that is unclearable with no control on screen to
   clear it.

Two more gates (Account Head note, Voucher Line marked Correct) are hardcoded with
no Audit Rules entity behind them, and the newer of the two is missing from both the
status-policy text and the auditor-facing workpaper grouping.

## The status engine in one page

`compute_and_persist_cleared_status()` — `routers/verification.py:403`. Called per
line from `save_line_items()`. A line is **Cleared** only if all six gates pass;
otherwise **Query**. A line with no account code or no active Category is neither —
it stays `NULL` ("Pending").

| # | Gate | Source of truth | File:line |
|---|---|---|---|
| 1 | Required documents complete | Session Rulebook's Category profile → `documents[]` | `verification.py:557-758` |
| 2 | Voucher-level checks all done | `audit_rule_voucher_checks` × `voucher_check_status` | `verification.py:816-841` |
| 3 | No signature exceptions | `voucher_line_documents.signature_exception` | `verification.py:855-863` |
| 4 | No hard-fail cross-check | `evaluate_cross_checks_for_line()` | `verification.py:873-886` |
| 5 | No pending Account Head note | Sentinel system observation | `verification.py:922-927` |
| 6 | Voucher Line marked Correct | Sentinel system observation | `verification.py:938-947` |

Gate 1 satisfies a Document four different ways depending on `document_type`:
`physical_bill` → bill completion + exact total match; `form` → field completion +
Category-resolved role ticks; template-backed → an active row in
`voucher_line_documents`; no-template → **all** of the Document's authorisation roles
ticked (`verification.py:696-706`).

The docstring at `verification.py:408` still says "five blocking conditions", as do
`static/verification.js:2957` and the status policy at `audit_rules.py:3381-3387`.
Gate 6 was added later and never propagated into any of them.

## Traceability table

| Rule type | Defined / authored | Reaches the engine? | Shown in Verification UI | Gap |
|---|---|---|---|---|
| Category → Documents (attachment) | `audit_rule_category_documents`; picker at `audit_rules.js:3341` | **Yes** — gate 1 | Modal rail tab per Document | No |
| Category → Document **"Required"** flag | Checkbox `audit_rules.js:3302`; `PUT .../required` `audit_rules.py:3792` | **No** — never read | Not shown; `cleared-requirements` filters *on* it | **Yes — A1** |
| Document type rules (template / no-template / physical bill / form) | `audit_rule_documents.document_type` | **Yes** — gate 1 branches | Per-tab evidence UI | No |
| Document authorisation roles | `audit_rule_document_auth_roles` | **Yes** — gate 1, no-template branch | Checklist, but built from a *different* rule | **Yes — C1** |
| Document **Required Authority** | `required_authority`, approval roles only | **No** — metadata by design | Stated as non-gating, `audit_rules.js:2795` | No (declared) |
| Document **Transaction Level Approval** | `transaction_level_approval`; **no author UI** | **No** — engine never reads it | Read by 3 consumers, all inert | **Yes — A5** |
| Category → Procedure / Evidence checks | `validation_rules_json.procedure_evidence` | **Partly** — forms only | Drives the whole checklist for *all* types | **Yes — C1** |
| Category → Cross-checks + matrix | `audit_rule_cross_checks` + fields/cells | **Yes** — gate 4, honours operator/tolerance/blocking | Data Table segment, rail dot | No |
| Category → legacy `mathematical_cross_checks` | `validation_rules_json` | **No** — never evaluated | "Preserved but not editable", yet counted in the rule total | **Yes — A4** |
| Voucher-level Checks | `audit_rule_voucher_checks` | **Yes** — gate 2, globally | Voucher-checks bar | No |
| Voucher-level Check **authority** | `audit_rule_voucher_checks.authority` | **No** — "informational only" | Not rendered anywhere | **Yes — A2** |
| Voucher-level Check auth roles | `audit_rule_voucher_check_auth_roles` | **No** — dead table | Nothing | **Yes — A3** |
| Signature Specimens | `signature_specimens` | **No** gate; used to *label* checklist entries | Signer names in checklist labels | No (declared) |
| Signature exception | `voucher_line_documents.signature_exception` | Gate 3 exists — writer has no caller | No control | **Yes — A7** |
| Account Head recommendation | Sentinel observation, no Audit Rules entity | **Yes** — gate 5 | Voucher Line tab | **Yes — B2** |
| Voucher Line "Correct" | Sentinel observation, no Audit Rules entity | **Yes** — gate 6 | Voucher Line tab | **Yes — B1** |

## A. Defined but never read by the status engine

### A1. The Category → Document "Required" flag does nothing — and the summary screen believes it does
**Confidence: High. Evidence: code, both sides. Severity: Fix.**

`audit_rule_category_documents.is_required` is a first-class, live setting. It has a
real checkbox (`audit_rules.js:3302-3316`), a dedicated endpoint that bumps
`matrix_version` on change (`audit_rules.py:3792-3816`), and it is compiled into the
Rulebook as each document's `required` field (`audit_rules.py:504`).

The engine builds its requirement set from **every** document in the Category
profile and never looks at `required`:

```python
required_docs = [ {**doc, "document_id": ..., "is_active": True}
                  for doc in (category_profile or {}).get("documents", []) ]
required_doc_ids = {d['document_id'] for d in required_docs if d['is_active']}
```
`verification.py:569-573`

`get_category_documents()` still carries the original admission of this, unchanged:
"Not yet read by `compute_and_persist_cleared_status()` — that wiring is Phase 7's
job, deliberately deferred" (`audit_rules.py:2905-2909`). Phase 7 shipped; this did
not.

The second half is worse than the first. `GET /categories/{id}/cleared-requirements`
— the read-only "what does Cleared require for this Category" endpoint behind the
Category Control Center — **filters on the flag** (`audit_rules.py:3367-3369`). So
unticking Required removes a Document from the list the auditor is shown while
leaving it fully blocking. The two screens now state opposite things about the same
Document.

### A2. Voucher-level Check "authority" is stored and never used
**Confidence: High. Severity: Defer (declared intent).**
`audit_rule_voucher_checks.authority` is authored via the API, carried in the
Rulebook (`audit_rules.py:404`), and self-documented as "Informational only, not
matched against anything yet" (`audit_rules.py:3417-3419`). The voucher-checks bar
renders only `check.name` plus tick/cross (`procedure_modal.js:2254-2260`), so it is
not even displayed.

### A3. `audit_rule_voucher_check_auth_roles` is a dead table
**Confidence: High. Severity: Defer (cleanup).**
Table plus two helpers retained, explicitly "kept defined but no longer called"
(`audit_rules.py:3426-3444`). No reader, no writer, no UI.

### A4. Legacy `mathematical_cross_checks` inflate the Category rule count
**Confidence: High. Severity: Fix (small).**
Category `validation_rules_json.mathematical_cross_checks` is validated on save
(`audit_rules.py:2416`) and parsed into the Rulebook (`audit_rules.py:161-166`), but
nothing evaluates it. The UI is honest about this in one place — "N legacy Category
comparison(s) are preserved but not editable" (`audit_rules.js:3858-3860`) — and
dishonest in another: the same count is **added into the Category's displayed rule
total** at `audit_rules.js:2616`, so the Control Center reports rules that can never
affect a status.

### A5. "Transaction Level Approval" has no author UI, and the engine ignores it anyway
**Confidence: High. Severity: Fix (decide) — two independent problems.**
The checkbox is intentionally hidden (`audit_rules.js:833-835`), so
`audit_rule_documents.transaction_level_approval` is permanently `FALSE` for every
Document and `toggleDocumentTransactionLevelApproval()` (`audit_rules.js:836`) has
no caller. That much is already recorded in `CLAUDE.md` and is confirmed still true.

What is **not** recorded: three live consumers read the flag —
`_compute_line_procedures()` (`verification.py:3413`), the modal's rail dot
(`procedure_modal.js:2514`, `:2584`) and the Workbook badge
(`audit_workbook.js:878`, `:938`) — all permanently inert. And the status engine
does not read it at all: gate 2 applies the voucher-level checks to **every** line of
**every** Category unconditionally (`verification.py:816-841`). So even if the
checkbox were restored, the display logic and the gate would disagree about which
Documents the voucher-level checks apply to.

### A6. Signature Specimens and Required Authority are metadata — by design
**Confidence: High. Severity: none (recorded so it is not re-reported).**
Specimens resolve and label checklist entries
(`audit_rules.py:237-308`, `procedure_modal.js:2388-2429`) and appear in signer
labels. Neither they nor `required_authority` gate anything, and the UI says so
outright (`audit_rules.js:2795`). Worth stating plainly: **nothing anywhere checks
that a signature actually present matches a specimen** — the specimen is a reference
list for the auditor's eye, not a control.

### A7. Gate 3 (signature exceptions) can never fail
**Confidence: High. Severity: Defer (already known).**
The only writer of `voucher_line_documents.signature_exception` is
`POST /api/verification/set_signature_exception` (`verification.py:4504`), reachable
only from `toggleSignatureException()` (`procedure_modal.js:5846`), which has **no
caller anywhere in `static/`**. Confirmed by grep. Gate 3 is therefore a permanent
pass. Matches the existing `CLAUDE.md` note.

## B. Status logic hardcoded with no configurable rule behind it

### B1. Gate 6 "Voucher Line marked Correct" is invisible to both summaries
**Confidence: High. Severity: Fix.**
Gate 6 (`verification.py:938-947`) blocks Cleared on every line and has no Audit
Rules entity behind it — it cannot be turned off, scoped, or configured. It is also
missing from two places that claim to explain the status:

- `get_category_cleared_requirements()`'s `query_conditions` lists five conditions
  and omits it (`audit_rules.py:3381-3387`).
- `_build_working_paper_gates()` groups only `voucher_checks`, `documents`,
  `signature`, `cross_checks`, `account_head` (`verification.py:2982-2986`). Gate 6
  belongs to no group, so **every workpaper group can read "pass" on a line that is
  Query.** This grouping is what `verification.js:3013` and
  `procedure_modal.js:570` render.

### B2. Gate 5 "Account Head recommendation" is likewise unconfigurable
**Confidence: High. Severity: Feature.**
Same shape as B1 — always on, no rule behind it. It *is* listed in the status policy,
so only the configurability point applies.

### B3. Voucher-level checks are global, with no per-Category scoping
**Confidence: High. Severity: Feature.**
Every active check applies to every voucher regardless of Category. The one field
that could have scoped this is `transaction_level_approval`, which the engine does
not read (A5).

### B4. Physical Bill and Form completion thresholds are not configurable
**Confidence: High. Severity: Feature.**
`_evaluate_physical_bill_completion()` (`verification.py:6275`) and
`_evaluate_form_bill_completion()` (`verification.py:6340`) hardcode: every bill's
Bill No / Date / Vendor non-blank, at least one item, and the grand total equal to
the voucher amount within a float-safety epsilon only (no business tolerance). This
is a deliberate hard lock, but note the asymmetry — Cross-checks expose `operator`,
`tolerance`, `blocking` and `missing_data_behavior` per matrix row
(`verification.py:6688-6695`) while this equally-blocking rule exposes nothing.

### B5. A no-template Document with zero roles is permanently unclearable
**Confidence: High. Severity: Fix.**
`_no_template_doc_satisfied()` returns `False` when the Document has no roles
(`verification.py:701-706`). The Documents editor allows saving such a Document and
attaching it to a Category, with no warning. Any line in that Category is then Query
forever, and the modal offers nothing to tick.

## C. The engine and the UI check different rules

### C1. Procedure roles: two different definitions of "all procedures done" — HIGHEST IMPACT
**Confidence: High. Evidence: both implementations read side by side. Severity: Fix.**

*Backend*, no-template Document: requires **every** role in
`audit_rule_document_auth_roles` to be ticked —
`get_document_auth_roles_batch()` at `verification.py:696`, consumed at `:701-706`.
The Category's Procedure/Evidence resolution (`_procedure_roles_for_document()`,
`verification.py:94`) is applied **only to `document_type == 'form'`**
(`verification.py:583-589`).

*Frontend*, all document types: renders and requires only the roles that
`getProcedureChecklistEntries()` resolves from the Category's procedure checks
(`procedure_modal.js:2376-2444`), which is the source for every checklist render
(`:3467`, `:4331`, `:4898`, `:5771`) and for `isProcedureDocumentComplete()`'s
no-template branch (`:2580`).

The switch between the two modes is **Category-wide, not per-Document**:
`procedure_rules_configured = procedureChecks.length > 0`
(`verification.js:2092`), mirroring the backend's `category_procedures_configured`
(`verification.py:582`). Consequences, in increasing severity:

- Category has procedure checks covering a *subset* of a no-template Document's
  roles → the auditor ticks everything on screen, the dot goes green, the line stays
  Query with "(2 of 3 required roles ticked)".
- Category has procedure checks for *some other* Document → the no-template Document
  gets an **empty checklist**, zero tickboxes rendered, while the backend still
  demands all of its roles. There is no way to clear that line from the UI at all.

### C2. The rail dot blocks on things the engine does not
**Confidence: High. Severity: Fix (pick one truth).**
`getProcedureDocumentStatus()` (`procedure_modal.js:2503-2527`) turns a Document
amber on any of: an observation being present, a cross-check `shortfall`, a
cross-check `not_evaluated`, or a Data Table amount mismatch. The engine blocks on
**none** of these — gate 4 counts only `status == 'fail'` and says so explicitly
(`verification.py:873-886`), and there is no observation gate or amount-mismatch gate
anywhere. The same screen therefore shows a Query-looking Document on a line the
engine will mark Cleared.

### C3. Two frontend helpers disagree with each other
**Confidence: High. Severity: Fix (small).**
For the same Document, `getProcedureDocumentStatus()` treats "no procedure entries"
as complete (`!entries.length ||`, `procedure_modal.js:2478`) while
`isProcedureDocumentComplete()` treats a no-template Document with zero entries as
incomplete (`roles.length > 0 && …`, `:2581`). The rail dot and the default-tab
selection can reach opposite conclusions.

### C4. "Five gates" survives in three places
**Confidence: High. Severity: Defer (comment drift).**
`verification.py:408`, `verification.js:2957`, `audit_rules.py:3381-3387`.

## D. Rules that are silently dropped

### D1. A Cross-check with an unattached or deactivated Document never runs, and nothing says so
**Confidence: High. Severity: Fix.**
A Cross-check is excluded from evaluation if any of its Documents is not attached to
the Category, or has been deactivated — in the Rulebook compiler it is skipped with a
`cross_check_dependency_invalid` warning (`audit_rules.py:546-554`), and in the SQL
fallback via `NOT EXISTS` (`verification.py:6443-6454`). Authoring-time validation
prevents creating this state, but detaching a Document or deactivating one *later*
produces it.

The Rulebook's `warnings` array is the only signal, and **no file under `static/`
reads it** (confirmed by grep). The Category looks fully configured; the cross-check
simply never fires.

### D2. Deactivating a Category empties two gates for lines already frozen to it
**Confidence: High. Severity: Fix.**
Once a line is verified, `category_id_applied` is frozen and keeps governing it, and
the comment at `verification.py:486-490` states a since-deactivated Category still
resolves. But `compile_verification_rulebook()` compiles **active Categories only**
(`audit_rules.py:354-357`), so the frozen line finds no profile:

- `category_profile` is `None` → `required_docs = []` → **gate 1 passes vacuously**
  (`verification.py:567-573`).
- `evaluate_cross_checks_for_line()` is called with the explicit `category_id`, whose
  lookup is `… WHERE id = %s AND is_active = TRUE` → no row → returns
  `evaluated: False, results: []` → **gate 4 passes vacuously**
  (`verification.py:7109-7118`).

A line that was Query purely for missing evidence can therefore flip to **Cleared**
on its next save, simply because someone deactivated the Category. Reachable in
normal use: `delete_category` only requires that no verification session is active at
that moment, and the recompute happens in a later session.

## Not a gap — declared design, do not re-report

- Signature Specimens and Document Required Authority are audit metadata, stated as
  non-gating in the product itself (`audit_rules.py:3392-3395`, `audit_rules.js:2795`).
- Cross-check `shortfall` and `not_evaluated` deliberately do not block Cleared; they
  are logged as observations instead (`verification.py:873-886`, 909-920).
- A Category with zero configured requirements clears vacuously — consistent with the
  empty-set convention used by every gate.
- A line with no account code or no active Category is `NULL`/Pending, not Query.

## Suggested reading order if this becomes a fix phase

A1 and C1 are the two that change what an auditor sees on a real voucher today. D2
is the only finding that can turn a Query line into a wrongly **Cleared** line, so it
ranks with them despite needing an admin action to trigger. B1's missing workpaper
group is a reporting correctness issue on the same screen. Everything else is
cleanup or a feature decision.
