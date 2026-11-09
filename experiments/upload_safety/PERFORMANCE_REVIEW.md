# Upload performance review — 2026-11-09

Folders 1–6 only; Folder 0 excluded. All 33 original XLSX files were hashed and copied; twelve representative small/large files and controlled variants were exercised. Imports used a fresh guarded loopback PostgreSQL/app pair. Working-app access was limited to authentication, configuration reads and Data Bank GETs. No packages were installed.

## Measured results

Seconds, on this Windows machine. Browser figures are medians of three trials: one fresh browser context, then two Reset/reloads. They are not cold-disk or hosted-server benchmarks. Both versions used the same copied files; baseline router/commit evidence is retained privately. Validation/import figures are single observations; unchanged figures are three-run medians.

| Large workbook | Detail rows | Browser file → date feedback, before → after | Validate after | Import after | Unchanged validation after |
|---|---:|---:|---:|---:|---:|
| Transaction Report | 73,325 | 79.47 → 34.19 | 62.38* | 88.42 | 65.96* |
| Purchase Order | 11,123 | 17.86 → 6.61 | 28.10 | 21.79 | 24.19 |
| Purchase Demand | 13,363 | 23.58 → 11.36 | 22.39 | 26.11 | 22.42 |
| Gate Pass | 15,422 | 24.89 → 13.64 | 24.01 | 28.86 | 20.99 |
| Store receiving | 19,596 | 31.96 → 13.12 | 28.74 | 32.27 | 30.79 |
| Vendor Bill | 8,079 | 15.99 → 6.42 | 13.72 | 12.66 | 13.26 |

*The large Transaction Report correctly requires acknowledgement of a 0.01 source/detail difference. Its preliminary confirmation pass adds 28.90 seconds initially, and a 28.19-second median on repeats; the table shows the subsequent validation separately. Source figures were not adjusted.

Large GRN API mapping fell from 6.44 to 0.77 seconds, initial validation from 39.55 to 28.74, import from 39.99 to 32.27, and unchanged validation from 41.29 to 30.79. Purchase Order initial validation varied rather than improving consistently; no universal validation/import speedup is claimed.

Local populated Data Bank page 1/2/1 medians ranged from 0.15 to 2.11 seconds. All 36 working-app preview GETs succeeded in 0.54–2.14 seconds, with metrics status `ok`. Only Transaction Report, Purchase Order and Vendor Bill had populated broad scopes; empty scopes do not establish large-data performance. Hosted import/network-failure stress was not performed.

## Repairs and remaining costs

- Mapping reads headers only, retaining missing-sheet fallback and existing header interpretation.
- With Footing awaiting confirmation, XLSX date-format samples stop after three nonempty values per mapped Date column; sparse columns scan further. Samples cannot define coverage. Confirmed suggestions still scan the entire worksheet; legacy XLS keeps its reader.
- Pure date interpretations use bounded, complete keys; strict fingerprints are reused within one document comparison. Raw amounts, date ambiguity, repeated lines, validation plans, quotas and reconciliation remain authoritative.
- A separate profiled Purchase Order repeat fell from 68.87 to 43.75 seconds; `strptime` calls fell from 155,722 to 2,228. Profiling adds overhead and is separate from browser/API timings.
- Full workbook/error scans, comparisons, SQL controls and large response serialization still cost time. Local quota-size queries alone commonly took roughly 1–1.6 seconds; their safeguards were retained.

## Setup boundary and evidence

Working Transaction Report, Purchase Demand, Gate Pass and Vendor Bill lack Document Date roles. Only isolated clones received Transaction Date roles; Gate Pass/Vendor Bill also received required header mappings. Existing populated-template date interpretation needs separate owner/migration review. These fixtures do not certify current working-template readiness.

Private run context, hashes, copied sources, individual trial results, profiles, screenshots and regression XML live under ignored `private/performance_review/`; `latest.txt` identifies this run. Earlier configuration blocks and runner corrections remain in that evidence; settled before/after trials and latest successful fixture cases supply the figures above. The opt-in `performance_review.py` runner guards database name, loopback port, cluster directory and marker before mutations, and supports integrity verification and shutdown.

## Verification and acceptance

Passed: twelve import cases with exact independent SQL controls; three unchanged reuploads each; 36 browser trials per version; six-folder rename/reorder/correction/deletion/partial checks; PO repeated lines, whole-voucher/empty deletion, overlap and simultaneous delivery; twelve confirmed full-sheet date ranges. All 33 original hashes match; isolated services are stopped and evidence retained.

Final regression selection: **625 passed, plus five subtests**. With separate scope/approach approval, four existing test modules were aligned with current backup settings, configuration locks, temporary staged workbooks, template roles, format-aware confirmed periods, inline errors and preview controls. Invalid-cell/no-write, multiplicity, freshness, scope and rollback assertions remain. Earlier interrupted/failing attempts are retained privately; `regressions-after.xml` records the final successful run. The existing Starlette/httpx deprecation warning remains; no installation is proposed.

Aadil's local-server acceptance and explicit commit/push authorization remain pending. Preview parity acceptance and two-decimal report display stay deferred.
