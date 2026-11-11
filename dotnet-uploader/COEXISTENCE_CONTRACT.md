# P06 — Migration routing contract

P06 rehearses route ownership with synthetic HTTP responses. The target remains a complete C# backend, including reports; see [current ownership](MIGRATION_CONTRACTS.md#5-c-report-ownership). This bridge serves staged migration and rollback, not a separate Python reporting service.

## Ownership and access

- Production registers no bridge routes; `Coexistence:Enabled` and authentication default to `false`. Existing health endpoints remain local. Settings alone cannot register forwarding routes.
- Server code registers an immutable ID, HTTP method, exact canonical path, owner (`DotNet`/`Legacy`) and access category (`Public`/`Authenticated`/`Administrator`/`Client`). No browser parameter, header or configuration value selects a backend URL or registers a route.
- Duplicate IDs/normalized method-path pairs, reserved health paths, templates, ambiguous paths and unsupported methods fail registration. Case/slash variants of a registered path are rejected before ASP.NET's permissive matching can bypass its gate. Unknown paths never forward; unowned methods on registered paths return 405.
- The access gate runs before either backend receives a registered operation. Unavailable/unauthenticated/forbidden decisions return 503/401/403. Test gates/controllers exist only in the test assembly. Stub decisions prove dispatch behavior, not real session, client/object authorization or CSRF enforcement.
- P07 validates shared active sessions, roles and configured-origin mutation checks. P07C's internal team eligibility is not an action permission. Client routes remain blocked until tenant membership, engagement assignment, configured sharing/visibility and object scope are proven for each route. Both backends must enforce equivalent access before public switching; forwarded identity headers are never trusted.
- P08's native user API is disabled by default. Do not route or enable it for live use until reviewed schema, administrator mapping, tenant selection, legacy parity and cross-backend enforcement are accepted.
- P09 client CRUD/selection defaults off; home-workspace and assigned-client permissions cover client records/selection only. Data/report routes stay blocked; mapping, picker, parity and cross-backend authorization precede live activation.
- P10's shell defaults off. Its selected-tenant session cannot use protected bridge routes under legacy global roles; prove equivalent selected-context enforcement before permitting forwarding. Packaged pages do not transfer ownership of their later-module APIs.
- P11 settings default off and require selected-context checks; never classify settings as public. Native tenant settings/policy cannot replace legacy global reads until upload/report consumers agree on scope, policy freshness and permissions. No route registration or shared-store cutover occurs here.
- P12 columns default off. Native labels differ from fixed storage keys; do not switch column/template writers until all consumers use the approved ID/key bindings, ownership, configuration locking and freshness rules. Legacy global APIs cannot safely consume native labels/keys unchanged; shared-schema execution and live routing remain gated.
- P13 templates default off. Native responses carry labels and fixed ID/key bindings with revision checks; legacy consumers cannot interpret these unchanged. Candidate schema, verified legacy ownership/bindings, consumer parity and single-writer activation remain separately gated.
- P14 saved mappings default off and protect only save/load/clear header choices. Their action grant, selected-client checks and native revisions do not activate staging/import/report routes. Retain one writer and verify legacy bindings/consumers before live switching.

## Local transport and limits

- Explicit target format is `http://127.0.0.1:<port>/`, port 1024–65535; credentials, alternate hosts, paths, queries and fragments are rejected. Self-forwarding to the API's listening port is blocked. Remote/container topology needs separate review.
- Defaults: total deadline 30 seconds (including body transfer), connect timeout 3 seconds, four concurrent transfers, request limit 8,388,608 bytes and response limit 16,777,216 bytes. Validation caps them at 120/10 seconds, 16 transfers and 32/64 MiB. These are rehearsal limits, not accepted upload/export capacity; later module phases review required sizes.
- One reusable HTTP client uses bounded connections, no cookie jar, no automatic redirect, no proxy and no automatic decompression. Requests are buffered within the size/deadline limit before dispatch; busy requests return 503 without queueing. Responses stream within byte/time limits.
- Preserve bodies, query parameters, approved content/download/cache/conditional headers and the `session_token` cookie only. Strip unapproved and Connection-nominated hop headers. Session issuance requires valid token shape, HttpOnly and local path/domain rules; expiration-only logout cookies are preserved. P07 owns final production cookie policy.
- Request content encoding/upgrades are rejected; OPTIONS, WebSockets and route templates are unsupported here. Same-origin root-relative redirects pass through; absolute legacy-origin redirects become relative. External/protocol-relative redirects fail. HEAD/204/304 remain bodyless.
- Protected or session-setting responses use `no-store`. Failures expose fixed reasons and a safe envelope; logs contain registered route ID/method/reason/outcome, never bodies, cookies, raw URLs, SQL or exception messages.

## Failures, rollback and activation

- Make one application dispatch with no retry or fallback to a second backend. An upstream HTTP rejection passes through. Transport failure before dispatch reports `not_sent`; after a mutation may have reached Python, report `unknown` and require status reconciliation. Read failures report `not_completed`.
- Timeouts return 504 before response start; transport/unsafe/oversized upstream failures return 502. Once a response has started, abort it rather than append another JSON reply. Client cancellation aborts; it never certifies rollback of a dispatched operation.
- Rollback explicitly changes registered ownership for subsequent requests and restarts the service. First drain in-flight work, reconcile uncertain writes and verify compatible data/schema; do not replay failed writes or overwrite newer history. Keep one writer per operation/scope.
- Activation requires P07 access/session parity, module-specific API/accounting acceptance, reviewed routing/limits/topology, and a tested rollback procedure. P06 changes no Python source, deployed schema, client records or live routing.

## Verification

Rehearse native/legacy ownership, default/denied access, path ambiguity, target validation, header/cookie/redirect rules, payload limits, cancellation, concurrency, interrupted responses and lost write acknowledgements. Use synthetic handlers plus an ephemeral loopback HTTP server; the real Python server is not a test target. Existing isolated PostgreSQL tests remain regression coverage; no deployed database access is authorized.
