# P05 Audit and Error Contract

Version 1, accepted by Aadil on 2026-11-11 with instruction to commit/push and proceed. Implementation, schema execution and historical-data access retain separate approval gates. Read with [P02](MIGRATION_CONTRACTS.md) and [schema ownership](POSTGRES_SCHEMA_OWNERSHIP.md).

## Existing evidence

Reviewed source matches the frozen [P01 baseline](migration-baseline.json); deployed contents remain unverified.

| Area | Current source contract |
|---|---|
| General audit | `database.py:log_audit` writes SQLite `audit_log` and suppresses failures. Fields: `id`, `user`, `action`, `module`, `details`, `timestamp`. `routers/users.py` changes PostgreSQL users, then separately logs the session username to SQLite. |
| Data-change audit | SQLite/PostgreSQL helpers write `data_audit_log`: row ID, actor text, action, compact old/new values and `changed_at`. INSERT stores no payload; UPDATE keeps changed fields; DELETE keeps a fingerprint. Full prior-row snapshots belong to `client_data_history`. |
| Errors and registry | General `log_error` and registry lookup use SQLite. PostgreSQL declarations already exist for `audit_log`, `data_audit_log`, `error_log`, `error_codes`; declarations do not establish live presence or usage. Error `user_id` is text; `error_code_id` is advisory, without a declared FK. |
| Capture and display | `main.py` supplies System/null attribution for unhandled errors and captures JSON/query inputs and technical details. Error routes use SQLite; the dashboard consumes their fields. Existing capture/display behavior is evidence, not the target privacy/access policy. |

## Required target behavior

- .NET writes PostgreSQL only. Reuse compatible deployed objects after catalog review; do not create parallel audit stores or recreate tables merely because source declares them.
- Take actor ID, username snapshot and authorized client/object scope from trusted server context. Distinguish authenticated, anonymous, system and imported/unknown actors. A submitted username at failed login is unverified; cookies/tokens and browser-supplied `resolved_by` never establish attribution.
- Preserve historical actor text without upgrading Admin/System labels to verified identities. P05B's candidate SQL adds nullable actor, event/operation, correlation and import-provenance columns; old records remain unclassified. Legacy fields/IDs/FKs remain compatible; shared-schema execution retains the schema gate.
- Required audit events for migrated business/security mutations share their `PostgresTransactions` context and commit. Audit failure rolls back the mutation; swallowed SQL errors cannot produce success. Record rejected/failed attempts as such, without claiming a completed change. Module phases enumerate required events and integrate these services.
- Diagnostic errors use a separate bounded attempt after the failed business transaction has ended. P05B defaults to a two-second attempt (configurable 1–10), plus P04B's bounded cleanup. Persistence/registry failure preserves the original response/failure; fallback logs only persistence state, event UUID and numeric status. No diagnostic write replay or recursive logging.
- A persisted error receives its real error ID; otherwise retain null and use a distinct correlation identifier internally. Unknown commit outcomes require operation/receipt reconciliation before replaying either business or audit writes, under P04B.
- Keep compact data-change audit semantics under P02; audit payloads never become durable full-row transaction mirrors. Preserve committed history and distinguish correction history from permanent purge.

## Privacy and public compatibility

- New capture uses an explicit field allowlist. Exclude passwords/hashes, cookies/session tokens, authorization/OAuth credentials, connection strings, raw request/workbook contents and raw SQL parameters from every database/console/public path. Sanitization failure omits unsafe detail; it must not fall back to raw serialization.
- Redact before bounding or formatting messages, traces, notes and metadata. Message/trace-tail/suggested-fix caps are 1,000/5,000/500 Unicode characters; new source-code/path capture stays off. P05B rebuilds trace symbols without exception-message/argument values; typed service records require approved safe strings, never arbitrary request or exception text.
- P05B limits: metadata 5,000 UTF-8 bytes (configurable 64–5,000), compact-value depth 8 (1–16), audit details 1,000 characters (1–1,000), actor name 256, symbolic labels 100 and correlation 64. Metadata uses numeric identifiers/counts and fixed outcome/stage values; omission/truncation remains valid JSON. Compact changed values require a server-owned field allowlist; sensitive/unapproved values are redacted. Oversized/unsupported required audit values fail the mutation rather than silently losing evidence.
- PostgreSQL `error_log.input_data` is JSONB; the legacy API expects nullable JSON text. Adapt safe values to that string contract rather than exposing an object or invalid truncated JSON. Preserve null/empty distinctions through fixtures.
- Preserve the five error-log routes, list-array shape, snake_case fields, booleans/nulls, numeric IDs, messages and stats keys. List defaults: skip 0, limit 50, maximum 200; the browser requests 20. Retain module/category/resolution/search/date filters and newest-first behavior; test timestamp ties. `/stats` must remain distinct from the numeric-ID route.
- Preserve safe validation/business errors and the unhandled-error envelope. Public `technical_details` may be null; do not expose internal paths, SQL, source or secrets. Registry code strings remain stable, including `ERR-UNK-001`; .NET exception mappings need explicit fixtures rather than guessed numeric registry IDs.
- Intentional security changes: active authenticated administrator access for error administration, server-derived resolution actor, CSRF protection on mutations, and safe text rendering of stored fields. P07 supplies authentication; P57 ports dashboard/API behavior. Error administration must not become public before those guards are ready.
- Resolve/unresolve/delete actions produce required audit events in the same transaction. Ordinary audit events are append-only; separately approved retention/purge follows P02. No silent age-based deletion, new retention period or storage increase is authorized; C01's 500 MB budget includes audit growth.

## Historical reconciliation and delivery gates

1. P05B implements PostgreSQL persistence, sanitization and failure handling against isolated fixtures. Verify audit rollback, compact-payload/fingerprint parity, false-success rejection, diagnostic/registry outage, root-cause retention, valid bounded Unicode JSON, secret exclusion and trusted attribution. Apply the existing schema-change gate before any shared-schema execution.
2. P05C rehearses source-aware reconciliation using synthetic/protected approved snapshots. Inventory SQLite and PostgreSQL records/registry entries, origins, counts and ID/code collisions; map each origin plus source ID once. Preserve IDs where compatible and record explicit mappings otherwise; verify references, sequences and repeat-safe restart without overwriting unrelated records.
3. Store new instants in UTC. Preserve source timestamp text/provenance for historical records; require an explicit conversion rule for naive/ambiguous times. Match legacy `dd/MM/yyyy HH:mm` API presentation with a reviewed display zone in P57, not the host's local timezone.
4. Compare sanitized fields, timestamps, counts, resolution states, compact payloads and registry mappings; account explicitly for omissions/redaction and any quarantined conflicts. Preserve protected source copies outside Git. Historical sensitive details must not be copied blindly into the active store.
5. Existing Python writers continue under their current ownership until a separately accepted handoff reconciles the final delta. Do not remove SQLite paths or switch traffic before that acceptance. P05C proves the rehearsal; actual export/import, deployment and retirement require named sources, capacity/backup evidence and separate approval.

Delivery: P05A/P05B are accepted; services/middleware and candidate SQL have local checks. Existing module writers, deployed schemas and historical records remain under the separately approved delivery gates above.
