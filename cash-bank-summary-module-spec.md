# Cash & Bank Summary Module — Rules and Logic Specification

Reference implementation: the Leather Crop combined cash flow (Jan–Jun 2026 and July 2026),
built from a raw voucher/transaction export plus a chart-of-accounts categories table.

This document is written to be handed to a code assistant (Codex / Claude Code) as the
functional specification for a `cash_bank_summary` module. Every rule below is one the
working report actually depends on. Where a rule was learned by getting it wrong first,
that is flagged as **Trap**.

---

## 0. Why the current build is failing

Three errors were reported. All three are intake problems, not logic problems.

| Error | Real cause | Fix |
|---|---|---|
| `58,052 journal row(s) have a blank voucher ID, date, or account code` | Accounting exports print the voucher ID and date **once per voucher**, on the first line only. Every continuation line is legitimately blank. The loader is treating them as invalid. | **Forward-fill** voucher ID and date down the rows before any validation (§1.4). Blank account code on a row that is a group total or a narration line → drop, do not error. |
| `78,188 row(s) have invalid dates` | Two separate causes: (a) the date column is a mix of Excel serial numbers, `dd-mm-yyyy` text and real datetimes; (b) Excel has silently converted some **account codes** into dates (`10-15` → `15-Oct`), so date parsing is being attempted on the wrong column or codes are corrupted. | Parse dates with an explicit cascade (§1.5). Repair date-corrupted account codes with a fix map (§1.6). |
| `More than one Client Data Bank dataset matches this client; select one upload or template` | Ambiguous dataset resolution in your own app: several uploads/templates carry the same client key. | Report generation must take an explicit `dataset_id` (or `upload_id` + `template_id`). Never resolve by client alone. If more than one candidate, return the candidate list to the UI for selection — do not guess and do not merge. |

**Governing principle for the whole loader: never abort a report because of bad rows.**
Quarantine them into a `rejected_rows` table with a reason, carry on, and surface the count
plus an amount total. A report that refuses to run teaches the user nothing; a report that
runs and says "Rs 412,000 across 38 rows could not be classified" is auditable.

---

## 1. Intake and normalisation

### 1.1 Required logical fields

Map the source columns onto these seven logical fields. Accept synonyms case-insensitively
and ignoring spaces/underscores.

| Logical field | Type | Accepted source headers (examples) |
|---|---|---|
| `voucher_id` | string | Voucher ID, Voucher No, Vou No, Doc No, Voucher # |
| `date` | date | Date, Voucher Date, Trans Date, Posting Date |
| `account_code` | string | Account Code, Code, GL Code, Acc Code, A/C Code |
| `account_name` | string | Account Name, Account Title, Head, Particulars |
| `narration` | string | Voucher Detail, Description, Narration, Remarks |
| `debit` | decimal | Debit, Dr, Debit Amount |
| `credit` | decimal | Credit, Cr, Credit Amount |

Optional: `bank` / `unit` / `branch` column — useful for reporting, never used for
classification (classification is by account code only).

### 1.2 Header detection

The header row is not always row 1. Scan the first 25 rows and pick the first row where at
least three of the required headers match. Everything above it is a title block — discard.

### 1.3 Junk-row removal (before validation)

Drop a row if any of these hold:

- `debit` and `credit` are both empty/zero **and** `account_code` is empty
- `account_name` (or the first non-empty cell) matches `^(total|sub[- ]?total|grand total|opening|closing|carried forward|b/f|c/f)\b` case-insensitively
- the row is a repeated header (its cells equal the header labels)
- the row is entirely empty

Count these as `dropped_layout_rows`, not as errors.

### 1.4 Forward-fill (critical)

```
last_vid = None; last_date = None
for row in rows_in_file_order:
    if row.voucher_id is blank: row.voucher_id = last_vid
    else: last_vid = row.voucher_id
    if row.date is blank: row.date = last_date
    else: last_date = row.date
```

Do this **in original file order, before sorting anything**. This single step removes the
58,052 "blank voucher ID / date" errors.

Only after forward-filling is a blank `voucher_id` a genuine defect.

### 1.5 Date parsing cascade

Try in this order, stop at the first success:

1. already a `datetime`/`date` object → use as-is
2. numeric and between 20000 and 60000 → Excel serial: `1899-12-30 + n days`
   (use the 1900 system; if the file came from legacy Mac Excel, `1904-01-01 + n`)
3. `ISO 8601` (`yyyy-mm-dd`, `yyyy/mm/dd`)
4. `dd-mm-yyyy`, `dd/mm/yyyy`, `dd-mmm-yy`, `dd-mmm-yyyy` — **day first**, because the
   source is Pakistani
5. `mm-dd-yyyy` only if step 4 produced an impossible date

Reject only if all five fail. Then sanity-check the result against the reporting period and
flag `date_out_of_period` rows separately — they are usually a year typo, not a parse failure.

**Trap:** do not let a permissive parser read `03-07-2026` as 7 March. Lock day-first.

### 1.6 Account-code normalisation

Account codes look like `10-15-115-15-0001`. Excel mangles the short ones into dates.

```
def normalise_code(v):
    if v is a datetime:
        if v.year == current_working_year:      # e.g. 2026
            c = f"{v.month:02d}-{v.day:02d}"    # 10-15
        else:
            c = f"{v.month:02d}-{v.day:02d}-{v.year % 100:02d}"
        return DATE_FIX.get(c, c)
    return str(v).strip()

DATE_FIX = {
    '10-01-15': '10-15',
    '10-20':    '20-10',
    '10-20-10': '20-10-10',
    '10-20-15': '20-10-15',
    '10-30':    '30-10',
}
```

Then: strip whitespace, collapse internal spaces, normalise dashes (en-dash → hyphen),
uppercase. Reject a code that does not match `^\d{2}(-\d+)+$` **after** repair.

The `DATE_FIX` map is client-specific. Store it as configuration per dataset, not in code,
and log every repair so the user can see what was changed.

### 1.7 Amount parsing

Strip thousands separators and currency text. Handle `(1,234)` and `1,234-` as negative.
Blank → `0`. Round to 2 decimals. Never use binary floats for stored balances — use
`Decimal` in Python and integer paisa or a decimal library in JS.

### 1.8 Row-level validity

A row is valid for the cash-flow engine when it has: a voucher ID, a parsed date in or
adjacent to the period, a normalised account code, and at least one of debit/credit non-zero.
Anything else → `rejected_rows` with a reason code. Report the count **and the rupee value**.

---

## 2. Reference tables the module needs

### 2.1 Categories table (chart of accounts) — one row per account code

| Column | Purpose |
|---|---|
`account_code` | key
`account_head` | display name
`fs_line_item` | financial-statement line the account rolls into
`tb_master_head` | trial-balance master grouping
`common_name` | **the cash-flow line the account appears on** — this is the driver of the report
`note_no` | optional; fixed note numbering the user controls
`section` | `MONEY_IN` / `NAMED_PAYMENT` / `BUSINESS_EXPENSE`

Rules:

- `common_name` is the only field that decides which cash-flow line an account lands on.
  Several account codes share one `common_name`; that is the whole point.
- Unmapped code → assign to a visible `(UNMAPPED)` line. Never silently drop and never
  guess from the account name.
- For an unmapped code, optionally offer a **longest-matching-prefix** suggestion from the
  mapped codes, as a suggestion in the UI only — not an automatic assignment.
- **Business rule:** creditors that relate to fixed assets are categorised under
  `Purchase of fixed assets`, **not** under vendor/trade-supplier payments.

### 2.2 Treasury register — which codes *are* cash and bank

```
CASH = { '10-15-110-0001',        # Daska unit cash
         '10-15-115-10-0001',     # Cash in hand
         '10-15-115-10-0003' }    # Online payments wallet
BANK = { '10-15-115-15-0001', '10-15-115-15-0002', '10-15-115-15-0003',
         '10-15-115-15-0004', '10-15-115-15-0005' }
TREASURY = CASH | BANK
```

These must be **configuration per client**, seeded from the categories table
(`common_name in ('Cash','Bank')`) and editable in the UI. Everything downstream depends on
this set being exactly right.

### 2.3 Opening and closing treasury balances

Taken from the cash books and bank ledgers, **independently of the voucher file**. They are
the control totals the report proves itself against (§5.4).

### 2.4 Voucher-prefix convention (informational only)

`BPV` bank payment · `BRV` bank receipt · `CPV` cash payment · `CRV` cash receipt ·
`JV` journal.

**Trap:** do **not** classify by prefix. Classify by whether the voucher actually contains a
treasury account code (§3.2). Prefixes are wrong often enough to break the report — the
reference engagement found bank payments posted on non-BPV vouchers and BPV vouchers whose
credit side included loan drawdowns and inter-bank transfers, which is exactly why the BPV
credit total did not agree to "bank money out".

---

## 3. Voucher classification

Work **voucher by voucher**, never row by row. Group all rows by `voucher_id`.

### 3.1 Per-voucher figures

```
cash_move = Σ (credit − debit) over lines whose code ∈ CASH
bank_move = Σ (credit − debit) over lines whose code ∈ BANK
treasury_lines    = lines with code ∈ TREASURY
non_treasury_lines = lines with code ∉ TREASURY
```

Sign convention: `credit − debit` on a treasury account is **money leaving** the account, so
a payment voucher gives a positive `cash_move`/`bank_move`.

Also check `Σ debit == Σ credit` per voucher. An unbalanced voucher is a data defect —
quarantine the whole voucher, report it, continue.

### 3.2 The four voucher types

```
if treasury_lines is empty:
        → NON_CASH JOURNAL. Exclude from the cash flow entirely.
          Count it and report the count. (Accrual JVs, depreciation, provisions,
          revaluations, year-end closing entries.)

elif non_treasury_lines is empty:      # pure treasury-to-treasury transfer
        has_bank = any line in BANK ; has_cash = any line in CASH
        if has_bank and has_cash:
                → BANK↔CASH TRANSFER. Keep, as a SELF-CANCELLING PAIR (§3.4).
        elif has_bank only:
                → BANK-TO-BANK transfer. EXCLUDE ENTIRELY.
        else:
                → CASH-TO-CASH transfer. EXCLUDE ENTIRELY.
        Count and report each of the three, with amounts.

else:
        → OPERATING VOUCHER. Allocate its non-treasury lines (§4).
```

### 3.3 Why inter-account transfers are excluded

They are not receipts or payments of the business; including them inflates both sides of the
statement. The user's instruction on this engagement was explicit: *ignore any inter-unit
cash and bank transfer.* In the reference month this removed 7 bank-to-bank vouchers and a
number of cash-to-cash vouchers.

### 3.4 The bank↔cash self-cancelling pair

Cash drawn from bank for office use genuinely reduces the bank column and increases the cash
column, so it cannot simply be dropped — the columns would not tie to their ledgers.
Treatment:

- Money In side: `Cash transferred bank to cash for office use (self-cancelling)`
  → bank `0`, cash `+transfer`
- Money Out side: `Cash withdrawn from bank for office use (self-cancelling)`
  → bank `+transfer`, cash `0`

Net effect on the total column is nil; the bank and cash columns each tie to their own
ledger. **Trap:** the sign is easy to get backwards. Cash = −(credit−debit) on the cash leg;
bank = +(credit−debit) on the bank leg. Assert that the two rows cancel in the total column.

---

## 4. Allocating an operating voucher to the bank and cash columns

For each non-treasury line in an operating voucher:

```
net = debit − credit                       # positive = expense/asset/payment
t   = |cash_move| + |bank_move|

if |bank_move| < 0.005:        bank_part = 0        ; cash_part = net
elif |cash_move| < 0.005:      bank_part = net      ; cash_part = 0
else:                          bank_part = net * |bank_move| / t
                               cash_part = net * |cash_move| / t
```

So a voucher settled wholly from bank puts the whole line in the bank column; a mixed
voucher splits every line pro-rata to how the voucher was funded. This is what makes the
bank and cash columns tie to their ledgers to the rupee.

Accumulate into `agg[common_name] = (bank_total, cash_total)` and, at the same time, into
`ledger[common_name][account_code] = (bank, cash)` and the entry list
`entries[(common_name, account_code)] = [ {voucher_id, date, narration, debit, credit} ... ]`.

### 4.1 `CASH_ONLY` override

Some accounts must only ever take the cash side of a mixed voucher. In the reference build,
commission accounts `60-10-90-0003` and `60-10-90-0004` had to be forced cash-only; without
it, General admin expenses was overstated by Rs 1,854,430 because the bank side of those
vouchers belonged elsewhere.

Implement as a per-client list `CASH_ONLY_CODES`, applied after the pro-rata step:
`bank_part = 0; cash_part = net`. Same shape for a `BANK_ONLY_CODES` list. Every override
must be visible in the UI with a reason — these are audit judgements, not code constants.

### 4.2 Sign convention for presentation

```
sign = −1 for MONEY_IN lines, +1 for everything else
displayed_amount = sign × accumulated_amount
```

A money-in account accumulates as negative `net` (credit side), so negating makes receipts
show positive. A refund or reversal inside a money-in head will show negative — correct, and
it should be left visible, e.g. `Bank loan repaid (net)` appearing as a negative receipt.

---

## 5. Report assembly — the Combined Cash Flow sheet

Layout, in order:

1. `OPENING BALANCE — CASH & BANK (date)` — bank, cash, total, from §2.3
2. **MONEY IN** block — one line per money-in `common_name`, in the user's fixed order,
   then the self-cancelling transfer-in line, then `TOTAL MONEY IN`
3. **PAID OTHER EXPENSES** block — named/one-off payees (the named-payment section), subtotal
4. `CASH AVAILABLE FOR BUSINESS EXPENSES` = `TOTAL MONEY IN − other-expenses subtotal`
5. **CASH/BANK SPENT FOR BUSINESS EXPENSES** block — one line per business-expense
   `common_name` in the user's fixed order, then the self-cancelling transfer-out line,
   then `TOTAL MONEY OUT`
6. `NET MOVEMENT` = `CASH AVAILABLE − TOTAL MONEY OUT`
7. `DERIVED CLOSING BALANCE` = `OPENING + NET MOVEMENT`
8. `Closing balance per the cash books and bank ledgers` (§2.3)
9. `DIFFERENCE` = 7 − 8 — **must be nil in all three columns**
10. An exclusions footnote: counts and amounts of bank-to-bank, cash-to-cash and non-cash
    journal vouchers excluded, and the self-cancelling transfer amount

### 5.1 Ordering and note numbers are user-owned

The order of heads and the note number against each head are **configuration**, not derived.
The user reorders lines and renumbers notes by hand and expects a refresh to preserve it.
Store `display_order` and `note_no` per `common_name` per client, and let a refresh update
only the amounts. A head with no data in the period drops out; a new head appends at the end
of its section for the user to position.

Also allow a per-client **display label** that differs from the `common_name`
(e.g. `Electricity bills` → `Electricity bills & power`).

### 5.2 Derived columns

- `Total (Rs)` = bank + cash (a formula in the export, not a hard value)
- `%age` = line total ÷ `TOTAL MONEY IN`
- `Amount in words` — Pakistani numbering: `≥ 1,00,00,000` → crore, `≥ 1,00,000` → lakh,
  otherwise thousand, to 2 decimals; negatives in brackets

### 5.3 Sub-note: what the trade-supplier payments were for

Payments to trade suppliers are meaningless as one number, so each supplier is traced to the
expense or stock head its purchases were charged to — the **contra head** in its ledger:

1. For every supplier account under the `Payments to trade suppliers & material` head, take
   that account's **credit-side** entries in the period (the purchases).
2. For each such entry, the contra heads are the accounts **debited in the same voucher**.
3. Allocate the payment made to that supplier across those contra heads **pro-rata to the
   debit amounts**.
4. Suppliers with no purchase in the period are only settlements of an earlier balance —
   group them by type of creditor (their `fs_line_item`) instead.
5. The sub-note must foot back to the head on the main statement, with a nil difference line.

Contra heads to exclude from this analysis (they are not expense or stock heads):

```
30-20-*       Financial Year P/L closing entries
40-15-10-*    Exchange revaluation gain/loss
10-15-95-*    Input sales tax
```

**Trap:** without the `30-20-*` exclusion, nine material suppliers get reclassified to
"Retained earnings" because the year-end P/L closing entry hits the same voucher.

### 5.4 Reclassification of a creditor by its contra head — the coverage test

When the module suggests re-categorising a creditor to the `common_name` of its contra head,
require that the identified contra heads account for **at least 40% of that ledger's gross
activity** in the period. Below that, leave it alone and flag it for manual review.

**Trap:** without this test, six import-LC accounts were proposed for reclassification to
"Financial cost" on the strength of a few thousand rupees of bank charges.

---

## 6. Ledgerwise Summary sheet

For each `common_name` in the statement's own order:

- a heading band carrying the note number
- one row per `account_code` under it: code, account head, bank, cash, total, `fs_line_item`
- a `Subtotal — <head>` row that **must equal** the same line on the Combined Cash Flow
- sort ledgers within a head by descending `|total|`
- suppress a ledger whose bank and cash are both < 0.005

Plus the contra-head column:

> **Contra head(s) in the ledger (account code)** — for each ledger, the distinct accounts
> appearing on the opposite side of the vouchers that ledger appears in, formatted
> `Head (code)`, largest amount first, semicolon-separated. Cap the list (10 is a good
> default) and append `and N other head(s)`, otherwise the cell becomes unreadable.

---

## 7. Entry-wise break-up sheets

For each ledger in Ledgerwise Summary order, a block containing every entry behind it:

`Voucher No | Date | Description | Debit (Rs) | Credit (Rs) | Contra head of the entry`

- include only entries from vouchers that had a treasury leg — the same population the
  summary is built from, so the block total ties to the summary row
- **per-entry** contra head: for a debit line, the accounts credited in the same voucher;
  for a credit line, the accounts debited. Same `Head (code)` format.
- a `Total — <ledger>` row per block; `debit − credit` must equal the ledger's
  `bank + cash` in the summary (absolute value; the sign differs by section)
- split across several sheets once a sheet passes roughly 1,500–2,000 rows; keep whole
  heads together where possible and name them `Ledger Break-up 1..N`
- carry a `Break-up sheet` pointer column on the Ledgerwise Summary so a ledger can be found

---

## 8. Self-check suite — run on every report, show the results

The report is only trustworthy because it proves itself. Implement these as assertions whose
outcome is displayed, not as silent tests.

| # | Check | Pass condition |
|---|---|---|
| 1 | Completeness | every valid row is either allocated, or in an excluded voucher class, or in `rejected_rows`. No row unaccounted for. |
| 2 | Voucher balance | `Σ debit == Σ credit` per voucher |
| 3 | Bank column tie | `opening bank + net bank movement == closing bank per ledgers` |
| 4 | Cash column tie | same for cash |
| 5 | Statement difference | the `DIFFERENCE` row is nil in bank, cash and total |
| 6 | Ledgerwise agreement | every `Subtotal — <head>` equals its Combined Cash Flow line |
| 7 | Break-up agreement | every ledger block total equals its Ledgerwise row |
| 8 | Sub-note agreement | identified + settlements equals the trade-supplier head |
| 9 | Transfer pair | the two self-cancelling rows net to nil in the total column |
| 10 | Unmapped visibility | `(UNMAPPED)` line is present whenever any code is unmapped, with its amount |

Tolerance: 0.02 (two paisa) on any comparison, never more. In the reference build a 32.73
difference looked like rounding and was **not** — it was a real Rs 33 credit on account
`10-15-95-0009`, tax on bank profit. Do not widen the tolerance to make a check pass.

---

## 9. Suggested module shape

```
cash_bank/
  intake/
    read_source.py        # header detect, column mapping, junk rows
    normalise.py          # forward-fill, dates, codes, amounts
    validate.py           # row validity, rejected_rows, voucher balance
  config/
    treasury.py           # CASH / BANK sets per client
    overrides.py          # CASH_ONLY_CODES, BANK_ONLY_CODES, DATE_FIX
    layout.py             # display_order, note_no, display labels per common_name
    exclusions.py         # contra heads excluded from the sub-note
  engine/
    classify.py           # §3 voucher type
    allocate.py           # §4 bank/cash split
    aggregate.py          # agg, ledger, entries
    contra.py             # §6/§7 contra heads, §5.3 sub-note, §5.4 coverage test
  report/
    cashflow.py           # §5
    ledgerwise.py         # §6
    breakup.py            # §7
    export_xlsx.py
  checks/
    selfcheck.py          # §8
```

Data flow: `source file → rows → normalised rows → vouchers → classified vouchers →
allocations → aggregates → sheets`, with the self-check suite reading the aggregates and the
control totals.

---

## 10. Non-negotiables — the short list

1. Group by voucher. Never classify a single row on its own.
2. Classify by the presence of a treasury account code, never by voucher prefix.
3. Forward-fill voucher ID and date before validating anything.
4. Parse dates day-first; repair date-corrupted account codes.
5. Exclude bank-to-bank and cash-to-cash transfers entirely; carry bank↔cash as a
   self-cancelling pair.
6. Exclude vouchers with no treasury leg from the cash flow, but count and report them.
7. Split mixed vouchers pro-rata to how they were funded.
8. `common_name` from the categories table is the only thing that decides a cash-flow line.
9. Creditors for fixed assets sit under fixed assets, not vendor payments.
10. Head order, note numbers and display labels belong to the user and survive every refresh.
11. Never drop an unmapped or rejected row silently — show it with its amount.
12. The report must prove itself to nil against the cash books and bank ledgers before it is
    shown as final.

---

## 11. Runtime performance and synchronization contract

The application implementation keeps raw uploaded `client_data` immutable. Account Control
Centre is the authority for Cash/Bank account selection and category mapping. Uploads and
configuration changes write a versioned derived transaction-line classification layer; they do
not rewrite the uploaded JSON rows. Add, edit, and deactivate operations reclassify affected
existing derived lines, with deactivation used instead of destructive deletion.

Cashflows must read that derived layer through SQL. The query applies the client, configured
Transaction Report template, current-row, non-zero, and requested date filters before Python
processing. It joins counter-account lines by voucher ID, keeps the complete selected voucher,
and performs the category/month aggregation in SQL. Python formats the already-aggregated rows
and only applies presentation details such as labels, review lists, transfer display, and
gross/net receipt wording. It must not repeatedly parse every raw JSON row for each report date
range.

The query may retain a safe compatibility fallback for fixtures or databases without a populated
derived layer, but production Cashflows must use the derived SQL path. Any fallback or degraded
database status must be visible in diagnostics; it must never alter or delete the raw upload.
