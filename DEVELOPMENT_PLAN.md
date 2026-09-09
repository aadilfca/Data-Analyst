# Development Plan

## Final combined closeout ? 2026-09-09

Aadil requested one final phase covering acceptance, safety, release checks, and Git closeout.
No further implementation phase is scheduled. The dashboard correction was manually accepted.

This is the sole current status register. Earlier phase tables and proposed next actions are
historical; they must not reopen completed work. See `CHANGELOG_ARCHIVE.md`,
"Final combined closeout - 2026-09-09". The previous roadmap remains available in Git at
`e6bfe11:DEVELOPMENT_PLAN.md`.

| Scope | Final development status | Evidence / limit |
|---|---|---|
| Form A-I | Implemented and accepted | Rendering, Copy from lines, typed values, blanks, migration, and legacy retirement completed. |
| Governance D1-D9 | Implemented and accepted | Canonical Category Documents, dependency validation, frozen Rulebook, migration safeguards, and inactive configuration handling completed. |
| P1 / P7 | Accepted; desktop checks passed | Verification and Workbook counts accepted; Category tabs and shared modal checked. Whole-page mobile width remains a recorded limitation. |
| P2-P6 / Safety A-B | Combined regression completed | 183 tests and 33 subtests pass; includes isolated presence, status gates, Forms, Cross-checks, save ordering, and recovery. Live reads supplement prior user acceptance. |
| P8 | Review completed | Existing measurements retained; further tuning requires a measured new issue. |
| P9 | Release review completed with recovery limitation | Health, historical logs, manual startup, and fixture isolation checked; full PostgreSQL backup/restore evidence unavailable. |
| Dashboard correction | User-tested, pushed, deployed | Render returns 33.9 MB / 87,943 active rows; PostgreSQL health ready. Implementation commit: `5373c72`. |

## Release assessment

| Phase No | Phase/Pendency | Objective | Status |
|---|---|---|---|
| This phase | Recovery evidence | Verify a recent full PostgreSQL backup and successful isolated restore, including Forms and Verification evidence. | Unverified; full release sign-off cannot be claimed. No new development phase is created. |

Export Backup covers templates/imported data/settings, not the full audit database. Old SQLite
backups do not prove recovery of current PostgreSQL data. No full organizational-data export or
destructive restore was performed.

Local startup is manual through the tray launcher; no matching automatic startup task or
Startup-folder entry was found. Running processes retain old Python code until restarted.
Browser checks used fixture client context and suppressed writes; they do not certify simultaneous
authenticated production editing or every live write path.

## Retained data and future work

Form 44 remains active with 86 migrated entries; inactive Vendor Bill 19 retains 43 source bills
across 42 lines. These are business records, not cleanup targets. This closeout created no live
test records; temporary test databases were cleaned up.

Unrelated historical findings and optional enhancements remain in `KNOWN_ISSUES.md`.
Completed/Cancelled session handling, further performance tuning, and broader mobile layout work
require a new brief; they are not follow-on phases of this plan.
