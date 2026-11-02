# Upload safety experiments — 2 November 2026

Baseline experiments identified three defects. Repairs are now implemented; results appear under “Repair verification” below. Baseline observations remain recorded here. Original workbooks and the working database remain unchanged.

| Source / control | Result |
|---|---|
| January–August voucher reports | 45,836 lines imported independently and into one cumulative client; counts, dates, account codes and debit/credit totals matched. Every repeat was a no-op. |
| Six cash/bank ledgers | 8,152 posting lines matched. Independent decimal calculations agreed with every running balance and opening/closing proof. Controls/continuation notes stayed separate. |
| Purchase demand, purchase order, gate pass, GRN and vendor bill samples | All five imported with matching counts/quantities/amounts after appropriate source/settings corrections. |
| Extended purchase orders and vendor bills | 8,028 and 3,748 lines imported with matching document multiplicities and numeric totals; repeats were no-ops. |
| All mapped-cell comparison | 22 original import datasets / 99,824 posting rows checked. Two Account Name differences represent the same June source error cell imported twice; see defect 3. Other mapped cells matched. |
| Duplicate protection | Renamed/repeated files, simultaneous imports, explicit duplicate line IDs and database uniqueness checked. Legitimate identical posting occurrences survived without repeat-upload growth. |
| Corrections and failures | Explicit line IDs allowed verified local archives, updates in place, real SQL rollback, retry of the same plan and receipt-safe repeated imports. Partial corrections retained absent lines. |
| Invalid sources/settings | Blank critical fields, malformed dates/numbers, missing mapping/account/staged files, wrong coverage, expired plans, changed source files, damaged/empty/HTML files and validation-bypass attempts were blocked. |
| Browser components | Actual backend row error, field highlighting/focus, error paging, safe text, Escape and source-row navigation passed offline Chromium checks. |

## Defects reproduced before repair

| Priority | Finding | Proposed short user message |
|---|---|---|
| High | Voucher `Infinity` passes numeric checking and fails while saving the validation plan with PostgreSQL invalid JSON. Router returns HTTP 500; no canonical rows were inserted. | “Debit Amount: enter a finite number.” |
| High | On the current fresh PostgreSQL bootstrap, replacements that remove old lines fail because cleanup references missing `cashflow_ledger_rollup_read_models`. Complete removal/empty-period clearing cannot complete. This does not prove an existing database has the same schema. | “Replacement unavailable. Ask the administrator to check database setup.” |
| Medium | June source Excel row 2 has `#REF!` in Account Name. It becomes null without a row warning. The independent and cumulative imports both reproduced this; accounting amounts/codes remained correct. | “Account Name: fix the Excel formula error.” |

## Source corrections and policy choices

- Several documents use `DD-MM-YYYY`; select that format. The initial `DD/MM/YYYY` attempts were correctly rejected.
- GRN/vendor bill unlabeled numeric footers were rejected; adding `TOTAL` in experiment copies resolved them. Purchase Demand's footer also contains a date and “Total store”; removing only that inspected footer from a copy resolved it. Complete-report mode correctly refused arbitrary exclusion. Originals were untouched; cached formula values were preserved in corrected copies.
- Voucher negative amounts are accepted. Ledger negative amounts are rejected. Decide the business rule for reversals before changing this behaviour.
- A ledger line with both debit and credit populated is accepted; balance differences produce warnings and still permit import. Decide whether these should block import.
- Suggested date error: “Use DD-MM-YYYY, or change Date format.” Suggested footer guidance: “Remove this total row from Excel, then upload again.” These are proposals, not code changes.

## Evidence and limits

- Real PostgreSQL 17, freshly bootstrapped by unchanged `database.init_db_postgres`, at `127.0.0.1:55439/upload_safety_experiment`. Synthetic clients/templates/Trial Balance accounts; explicit mappings. No working-app or Supabase connection was used.
- Real upload/validation HTTP routes and unchanged import handler/SQL transactions were exercised. Import background report jobs stayed queued; full app authentication, global exception middleware, Drive, downstream Cashflows and live-browser acceptance were outside these checks.
- Existing regression suite: **181 passed, 17 failed**. Eight fixtures reference nonexistent staged workbooks; nine assert older wording. No existing tests were modified. An initial local-archive-mode fixture mismatch was resolved by rerunning with the fixtures' mocked Drive mode; no real Drive writes occurred.
- Private `results.json` records exploratory attempts, including initial setup failures and subsequently corrected experiment fixtures. Assertion failures are not all application defects; do not treat its raw pass/fail ratio as a release score.
- `private/cell-comparison.json`, original hashes, staged/altered copies, local database and screenshot retain detailed evidence. JSON/logs/private files are ignored by Git. The test PostgreSQL server is stopped after verification.

## Running again

Use the project's existing `venv`; no packages were installed. These scripts are standalone and never imported by the app. They deliberately override `DATABASE_URL` before application imports and redirect legacy SQLite auditing to `private/audit-only.sqlite`.

With a fresh dedicated PostgreSQL cluster already running on the test port and database created, run `run.py --bootstrap`, then `run.py`, `additional.py`, `focused.py`, `ledger_checks.py`, `ui_checks.py`, `final_checks.py`, and `parity.py` using `venv/Scripts/python.exe`. Run `run.py --regression` separately. Each invocation is explicit opt-in; never change its DSN to a working database. Use a fresh test database for a complete repeat: accumulated experiment data can otherwise reach the application's storage guard. No cluster reset/deletion is performed automatically.

The baseline run made no application changes. The approved repair phase changes only upload input checking and replacement cleanup; duplicate/accounting policies remain unchanged.

## Repair verification — 2 November 2026

- `routers/import_export.py` checks original mapped `.xlsx`/`.xls` cells for actual Excel errors and nonfinite numeric values before comparison or plan creation. Errors include original row/column and a short fix. Partial exclusions, literal text and valid values retain their existing treatment.
- `services/storage_cleanup.py` checks for the retired ledger-copy table before replacement cleanup; existing legacy copies are still purged. No schema migration or duplicate-store recreation is introduced.
- **237 affected tests pass.** Tests cover error cells, nonfinite values, direct imports, missing/present retired tables, ledger fixtures, archives and duplicates. The `.xls` cell-type adapter is mocked; real-workbook runs use `.xlsx`.
- **24 PostgreSQL repair scenarios pass:** malformed inputs, scoped removals, empty-period confirmation, verified local archives, actual SQL rollback, receipt retries, simultaneous imports, explicit line-ID rejection, legitimate repeated lines, eight voucher months and ledger account isolation.
- All 45,836 monthly posting lines matched source counts/totals. June's original `#REF!` now blocks validation; only its experiment copy received a clearly synthetic replacement label. Two Daska account scopes retained 926 postings with zero balance differences and no repeat-upload growth.
- Fourteen duplicate/comparison functions match the committed version structurally. All 117 original workbooks and the working SQLite file retain their original checksums.
- Run `after_fixes.py --tests` for regressions and `after_fixes.py` for database checks. `--quick` refreshes targeted cases without rerunning the completed months. Detailed evidence: ignored `private/after-fixes-results.json`; latest result per scenario is authoritative. Initial runner expectation/name-shadowing failures were corrected and rerun.
- Existing offline browser checks apply to the unchanged UI. No live-app browser acceptance, real Drive writes or downstream report builds were performed. Private staging outside `uploads/` can retain experiment files with cleanup warnings; working upload files were untouched.

Backend implementation is complete; Aadil's local-server acceptance remains pending. The test server is stopped after verification. No staging, commit or push has been authorised for this phase.

## Aadil's review

- Restart/reload the local server and import a valid file in a disposable test client; verify counts and totals.
- In the local Upload page, leave Account blank and press Validate; check that the account field receives focus and a short message.
- Try an invalid date; check that the dialog identifies its column and links to the source row.
- Use a disposable test client for any manual repeat-upload check; the stored row count should stay unchanged.
- Try `Infinity` and an actual Excel `#REF!` in experiment copies; verify short row/column errors and no import.

The PostgreSQL replacement/rollback checks are backend tests Aadil may review without repeating. Ledger warning/reversal policies retain their existing behaviour. Local acceptance and explicit approval are required before commit/push.
