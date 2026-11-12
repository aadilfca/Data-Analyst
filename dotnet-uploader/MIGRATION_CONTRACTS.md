# P02 Migration Contracts

Version 3: P07A–P07C precede P08 to address tenant and audit-firm collaboration; detailed business rules remain open. The [P01 inventory](MIGRATION_BASELINE.md) and [source hashes](migration-baseline.json) remain historical evidence. Phase-specific approvals and runtime parity checks still apply.

## 1. Status and scope

- **Existing:** behavior observed in source or required by the project guide; verify it with isolated fixtures before replacing an endpoint.
- **Target:** proposed implementation contract for review; implementation still follows each phase's file/approach gates.
- **Decided:** Aadil selected a 500 MB Free-plan database ceiling on 2026-11-10. This supersedes the earlier 400 MB ceiling for migration planning; see section 4.
- Move all backend behavior, financial calculations, reports, workbooks and jobs to .NET; retain existing HTML/CSS/JavaScript. Python remains the legacy comparison/rollback application until replacements are accepted; no separate Python report service is planned.
- Preserve P01 as a historical snapshot. This contract supersedes its ownership proposals and pending-policy notes without rewriting captured source or changing the existing Python patch. P01–P05C implementation remains reusable.

## 2. Public API and access

The inventory contains 325 HTTP declarations, 324 unique method/path pairs, and all 18 routers. Every public route and report calculation moves to .NET. Capture actual request/response fixtures before routing traffic; this architecture contract is not a complete DTO schema for every route.

| Contract | Existing evidence and target |
|---|---|
| Route matching | Preserve methods, paths, query/form/body field names, defaults, omitted/null/empty distinctions, pagination, and ordering. Resolve duplicate GET / declarations by testing main.py's login/client-picker entry before dashboard router inclusion. |
| JSON | Preserve each endpoint's property names, value types, and business-result envelope. Framework-default camelCase or generic error conversion must not change the browser contract. |
| Validation | Preserve strict booleans/integers where declared, accepted legacy fields, multipart names, and current HTTP/business error shapes. FastAPI validation responses must not silently become ASP.NET defaults. |
| Failures | Distinguish HTTP failure from HTTP 200 business rejection. Preserve detail strings/objects, row/column errors, conflict/revalidate guidance, download headers, and safe user messages. |
| Reads/writes | Current api_utils.js retries eligible GET failures once; mutations are not retried. Preserve safe-read behavior; reconcile uncertain write outcomes instead of replaying writes blindly. |
| Identity | Initially share session_token, existing users/sessions, bcrypt hashes, the 10-hour expiry, and session-selected client. No forced ID/hash rewrite. |
| Authorization | Enforce active user, tenant membership, role, engagement assignment, approved sharing and object scope as applicable. Client selection is context, never proof of permission. |
| Firm administration | Keep administrator-only user/client mutations, setup, backups, and protected settings. Explicit firm-level routes need no selected client; client-scoped operations need validated scope. Verify administrator report-route exceptions individually. |
| Security changes | Secure production cookies, CSRF protection, inactive-user rejection on protected requests, and consistent guards are target hardening. Current login rejects inactive users, but get_current_user does not recheck is_active; coordinate browser changes in P07 rather than claim existing parity. |
| Public surfaces | Login and intended health/public-information pages remain explicitly public. Missing static authorization evidence is not permission to expose an endpoint; review indirect guards before cutover. |

High-risk request fixtures must cover login/current-client; ImportRequest; multipart Trial Balance validation/import; the embedded Verification save-and-verify bundle; and CashflowExportRequest's expected_fingerprint/expected_identity. Sources: [auth](../routers/auth.py), [uploads](../routers/import_export.py), [Trial Balance](../routers/trial_balance.py), [Verification](../routers/verification.py), [reports](../routers/reconciliation.py), [fetch utility](../static/api_utils.js).

### Tenant and collaboration direction

- A business or CA firm can be a tenant. Business users can include directors/CEO; firm users can include administrators, managers and internees. A firm can serve multiple businesses and engagements.
- An authorized business–firm relationship permits controlled collaboration on a defined engagement. Assigned audit staff and permitted business users see only the scope approved for them; membership or client selection alone grants no engagement access.
- P07A records the foundational workflow and user-reviewed Q&A below. Items explicitly deferred remain open; no automatic publication or sharing rule is assumed.
- P07B/P07C follow separately approved scopes. Preserve canonical transaction/history contracts, legacy compatibility and one active writer; rehearse migrations/rollback in isolated databases before any shared-schema execution.
- Every migrated operation, report, cache, job, export and download must enforce and carry the applicable authorized tenant/engagement context; prevent stale access after revocation. Reconcile safeguards across Python/.NET before switching routes.

### P07A workflow and confirmed decisions

- Current app: users select a client; uploads pass mapping/validation and controls; auditors record procedures, evidence and observations; Audit Workbook shows voucher-level Cleared, Query or Pending. These statuses do not by themselves define CEO access or final audit approval.
- Current firm practice: the audit team uploads ERP Excel files. Future ERP-database integration should support automatic data fetching; its scope, access, safeguards and implementation require a separate brief and approval.
- Intended engagement outcome: the manager reaches final agreement with management; the audit team records management's stance/response against each observation. CEO financial summaries start with the existing Balance Sheet, Income Statement and Cashflows reports, with a show/hide column for observations and responses. Report detail, timing, visibility, agreement process and authorization remain to be specified.
- Agreement alone does not resolve an uncorrected issue: reports continue to show it as outstanding. The assigned audit team verifies corrections with manager review before resolution. Historical report/evidence access after sharing ends is determined by the specific arrangement; preserve audit history and apply the approved access policy.
- Platform administrator Aadil configures control of access/sharing and CEO visibility/timing separately for each business and engagement. The CA firm's administrator or an authorized manager assigns/removes engagement managers and internees; platform and tenant administration are distinct.
- Business-record ownership is determined by the specific arrangement. For CEO financial summaries, verified means audit checks were completed and reviewed by a manager, limited to the specific checked data and period; outstanding observations remain visible. If a later upload changes verified data, mark the affected data as needing audit review again. Detailed evidence and change-scope rules remain to be agreed.
- Existing app data starts under Aadil's CA-firm tenant; connect businesses as their arrangements are established. This starting association does not decide legal ownership; inventory, mapping, rehearsal and rollback require separately approved migration work.
- A CA firm may work for a business before that business has a tenant or users and connect it later. When multiple firms serve one business, each sees only information authorized for its own engagement.
- Policy configuration, delegation and defaults require explicit decisions. Configuration alone must not silently grant access outside its approved tenant, engagement, role and object scope; record who changed access and preserve audit history.
- Deferred to relevant phases: ownership enforcement and existing-data mapping; verification evidence/change scope and report presentation; invitations, revocation and historical access; visibility controls; assignment exceptions; and consent. Do not enable an operation before its necessary rules are agreed and enforced.
- P07A–P07C and the disabled P08 backend are locally accepted; shared-schema execution, deployed access and activation retain separate approval.

### P07A reviewable questions and answers

These concise paraphrases preserve Aadil's intent for later review; “open” means no policy was selected.

| Question | Aadil's response / status |
|---|---|
| Who are the tenant types and users? | Direct businesses are tenants with directors/CEOs; CA firms are tenants with administrators, managers and internees, and their customers/audit engagements are the firms' clients. |
| Should a business and its auditor collaborate in one app? | Yes; controlled sharing is expected to be common, with audit firms primarily checking business data and CEOs accessing cleaned, verified information. |
| Who configures engagement sharing and CEO visibility, and who assigns staff? | Platform administrator Aadil configures access and CEO visibility per business/engagement; the CA firm's administrator or authorized manager assigns/removes managers and internees. |
| Who owns uploaded business records, and what exactly does “verified” mean? | Decide ownership by arrangement; agree the precise meaning and review process together. Ownership enforcement remains open. |
| What data source does the audit team use now, and what is planned? | The audit team uploads ERP Excel files now; future ERP database fetching is a separate later integration. |
| What happens in review, and what should the CEO receive? | Manager reaches final agreement with management and captures management's stance; CEO receives reports. |
| Where is management's response recorded, and what report layout is wanted? | Record it against each observation and show it alongside the auditor's observation; financial summaries should have a show/hide observations-and-responses column. |
| Which financial reports should start the CEO view, and who records the response? | Start with existing Balance Sheet, Income Statement and Cashflows; the audit team records management's response. |
| Does an agreed but uncorrected issue remain outstanding, and what happens to prior access after sharing ends? | Yes, it stays outstanding; historical report/evidence access is decided separately for each arrangement. |
| Who confirms a claimed correction, and how should observations link to summaries? | Assigned audit team verifies with manager review; summary linkage/presentation is for later joint decision. |
| What does “verified” mean to the CEO, and what period does it cover? | Checks completed and manager-reviewed; status applies only to the checked data and period. |
| Are sharing and CEO-visibility settings global or configurable per engagement? | Configure separately for each business and engagement. |
| If later uploads change verified data, what should happen? | Mark affected data as needing audit review again. |
| Where should existing app data start, and how should mixed verified/unverified summary status display? | Start under Aadil's CA-firm tenant; decide mixed-status presentation later. |
| Can a firm work before the business has a tenant, and can firms see one another's work? | Yes, allow the firm to work first; keep each firm's engagement separate. |
| Must a business approve connection to its own tenant before data can be shared? | No final rule selected: Aadil deferred the detailed consent/sharing workflow to later development and asked us to focus now on hard-to-retrofit foundations. |

This list records discussion, not blanket authorization to activate policies. Revisit open answers and confirm policy before implementing dependent workflows.

### P07B isolated tenant foundation

- Candidate SQL defines tenant identities, active user memberships and one home-workspace association per existing client record; this association is neither legal ownership nor an access grant. It seeds no users, tenants, clients or permissions.
- Internal membership lookup revalidates the shared session, active user, tenant and membership. It is not a public endpoint and proves no permission to access clients, engagements, reports or other tenants; future operations must revalidate their own authorization.
- Isolated fixtures rehearse the candidate against synthetic legacy-shaped records; repeated SQL execution fails rather than silently accepting an unknown schema. Existing global username/client-code uniqueness remains unchanged for later review.
- Runtime startup runs no SQL. A real data inventory, approved firm-tenant/user/client mapping, capacity, schema compatibility, rollback and explicit execution approval are required before applying any change to a shared database.

### P07C isolated engagement foundation

- Candidate SQL defines firm-held client engagements and manager/internee assignments, with database-enforced firm, client-home and membership relationships. A business can be recorded before it joins; no business-tenant connection or sharing grant is created.
- Internal eligibility rechecks the active session, user, firm, membership, client, engagement and assignment in one query. It has no public endpoint and grants no client-data or action permission; callers must revalidate applicable authorization in their own operation.
- Isolated fixtures rehearse rollback, repeat failure, firm separation, multiple engagements, invalid assignments, revocations and preservation of synthetic legacy records. No existing verification session is linked or changed.
- Existing client routes remain blocked; business sharing/consent, tenant administration, action permissions and audit trails for assignment changes require their own approved phases. Never infer legal ownership from a workspace association.

### P08 isolated tenant users

- Candidate SQL adds per-tenant admin/member roles without granting existing members admin automatically; global legacy roles remain separate. Runtime startup performs no DDL.
- Without P10, the default-off user API requires exactly one active tenant membership. P10 uses session-stored selection and rechecks its revision and tenant-admin access for every action; headers cannot choose the tenant.
- Create/edit and membership activation are tenant-scoped. Shared identities and legacy global administrators cannot have global names/passwords changed through tenant administration; legacy global administrators are not modified. Password resets for eligible accounts revoke their sessions atomically; membership deactivation is rechecked for that tenant without ending other tenants' sessions.
- Writes and tenant-attributed audits share a transaction. Existing Python last-admin behavior remains unchanged; the absence of a last-admin safeguard is a known limitation. Invalid request details redact bodies that may contain passwords.
- Current validation caps usernames at 256 UTF-16 units and passwords at 72 UTF-8 bytes; legacy compatibility and historical values require review. Invalid-request details redact raw input. Shared schema/catalog and explicit administrator mapping, complete legacy response parity, multi-tenant selection, cross-backend session enforcement, UI acceptance and separate activation approval precede cutover. Python remains the active user-management implementation.

### P09 isolated tenant clients

- Default-off native client CRUD/search/pagination and selection use sole membership without P10, or its server-stored tenant context when enabled. Tenant headers, global roles and cached selected-client claims grant no access.
- Tenant administrators manage only home-workspace client records. Assigned active audit staff may list/select active clients through an active engagement; this specific permission grants no report/data access. Business members and cross-tenant sharing await explicit policies; business administrators manage their home records.
- Preserve global client-code uniqueness, optional profile fields, list shape, unpaged legacy calls and paged X-Total-Count. Count/page share one SQL snapshot. Duplicate-code errors omit other-tenant details; full validation/error/timestamp parity remains an activation gate.
- Client creation/home association, edits, soft deletion/restoration, session selection and tenant-attributed audit writes are atomic. Inactive clients clear saved selections; current-client rechecks access and clears revoked selections. No permanent deletion, shared-data access or automatic engagement/assignment creation is implemented.
- No schema/data migration is executed. Catalog/mapping, cross-backend authorization, client-card/admin UI acceptance, sharing policies where needed and explicit activation approval precede live routing. P09 is locally accepted behind disabled settings; Python stays active.

### P10 isolated application shell

- Shell hosting defaults off and requires authentication/user/client settings. Package existing flat HTML/JS/CSS assets; expose no repository, upload, workbook or backup directory. Recheck session, selected tenant/role and applicable client selection before protected HTML; retain public login/info and allowlisted retired-ACC bookmark redirects.
- Candidate SQL adds nullable selected tenant and a session context revision; it seeds no tenant/membership and runs only in owned fixtures. Explicit workspace choice verifies active membership, clears client selection, increments revision and writes its audit atomically. Concurrent stale choices cannot both apply.
- P08/P09 recheck selected tenant and revision inside their transactions. Their shell requests require X-App-Context, a session fingerprint/revision assertion rather than a tenant selector or credential; reject absent/stale assertions, including route case/slash variants. Every later tenant-bound operation needs equivalent checks before activation.
- The .NET-only external bootstrap runs before retained scripts, clearing application sessionStorage/history and obsolete selected-client localStorage keys on session/workspace change. Cross-tab notifications, visibility/focus and restored-page checks invalidate stale documents; browser state grants no access. It adds workspace selection to NAV_MANIFEST and a visible switch link without modifying Python assets.
- Client-card intents remain allowlisted; shared-data access and later-module APIs stay unavailable. Schema/mapping/backup/execution, full parity and cross-backend enforcement retain separate activation gates. Shell/browser rehearsal is not deployment acceptance; remaining modules keep their planned phases.

### P11 isolated tenant settings

- Default-off settings APIs require the shell's selected tenant and revision. Active members read ordinary settings, holidays and report defaults; tenant administrators change them and read/change backup policy. These native permission changes are approved; legacy Python permissions remain unchanged.
- Candidate tables separate tenant settings/holidays without copying legacy SQLite/PostgreSQL rows or running startup DDL. Unstored keys use existing seed defaults in memory. Only known ordinary/report keys are exposed; generic writes cannot bypass dedicated Cashflows/backup routes or expose unknown/private keys.
- Preserve ordinary string values and precision, holiday date/label/yearly fields, API paths/envelopes, and legacy local-server-day report-default resolution. Dedicated endpoints validate dates/ranges and strict backup booleans; full malformed-request/error/timestamp parity and historical-value mapping remain activation gates.
- Each change and its tenant-attributed audit commit together; all four Cashflows defaults save atomically. The existing Save All form still sends separate per-key requests, not one batch transaction. Audit metadata excludes raw settings/labels; holiday reference deletion is scoped and audited.
- Shared Settings UI escapes saved values and shows native non-admin controls read-only using shell capability metadata; server checks remain authoritative. Python's existing UI flow remains editable as before. Native Drive controls stay hidden until their phase; no OAuth/backup execution is added.
- Backup policy is a stored tenant choice only until upload consumers enforce it and invalidate/recheck affected validation plans. Legacy writers still read their original stores. Do not activate settings/policy routing before catalog/mapping, compatible consumers across both backends, browser acceptance and explicit activation approval.

### P12 column identity and labels

- Aadil approved separating permanent column ID/storage key, editable display label and incoming ERP header. For new native columns, the master row retains the ID and a server-generated opaque key; its tenant companion supplies display name/category/notes. Existing master IDs/values and global storage-key uniqueness remain intact; companion label uniqueness is case-insensitive per tenant, including inactive definitions.
- Candidate SQL guards linked IDs, storage keys, tenant/origin bindings and retained definitions. Metadata revisions advance on definition edits; these are future cache/configuration versions, not source fingerprints. No legacy ownership/key binding is inferred, seeded or rewritten. Legacy rows remain hidden until explicitly mapped.
- Existing datasets need verified bindings to their actual stored keys, scoped by template/version and upload where necessary. Preserve raw current/history JSON, identities, fingerprints and evidence; never derive historical keys from today's label or bulk-rename stored keys. New imports must map source headers to permanent IDs/fixed keys.
- Text/Numeric/Date remain base types; Document ID/Date and Footing belong to template mappings. Unverified dependencies block label changes; P13 permits verified native template bindings. Type/deactivation also blocks on current/history key presence. Tenant-only annotations leave legacy master metadata unchanged.
- Members read their tenant catalogue; tenant administrators mutate it with live context checks and atomic audit. Existing paths/envelopes remain; native rows add storage_key/definition_version, and valid 1/0 activation inputs remain supported. Shared Columns UI retains Python editing and shows native member controls read-only.
- Usage reports only verified tenant template counts; absent references/data yield zero, while unverified dependencies require review. References use IDs/keys, never matching captions. Missing schemas fail closed. Writers validate active scoped IDs, share advisory lock 746102 and invalidate revisions/plans before activation.
- P13 onward must carry stable bindings through templates, imports, rules, reports, exports and caches, replacing free-text/name-based lookups such as Rate detection. Legacy binding review, complete consumer/parity checks, schema execution and explicit activation remain gates; P12 alone does not make legacy renaming safe or enable mixed writers.

### P13 tenant templates and bindings

- Disabled native template APIs require shell/columns; members read, tenant administrators mutate with live session/context checks and atomic mandatory audit. Tenant labels may repeat across tenants; existing global templates are not assigned or copied.
- New templates keep permanent IDs and opaque internal names; companion labels and immutable template kinds separate presentation from meaning. Initial Purchase Order/Store Receiving/Vendor Bill classification follows existing names; subsequent renames retain that classification and Rate Source applicability.
- Native mappings store selected tenant/column IDs and fixed keys with database ownership/key guards. Ordered roles, nullable comparison/review settings, ledger roles, subtotal defaults and existing response fields remain; responses add keys, versions and configuration_key. No transaction values or fingerprints are rewritten.
- Edit/delete/reorder/removal require X-Template-Revision; editor snapshots include current column revisions. Supersede ready validation plans atomically after template/column edits. Reorder requires a complete permutation; omitted role/comparison edits preserve saved choices. Ledger preparation validates/reuses scoped columns and creates missing definitions atomically with its template.
- Current/history data protects field removal, date changes and ledger meaning; a Document ID change requires equivalent nonblank values in both stores. Upload/evidence/saved-mapping dependencies guard affected changes and deletion; missing schemas fail closed. Soft deletion retains bindings and reserves names. Unverified legacy definitions require migration review.
- Shared editor preserves Python behavior, carries native bindings/revisions, refreshes before edits/previews and disables member mutations. Only verified native template dependencies permit label changes; other consumers, legacy binding review, deployed schema and explicit activation remain separate gates. Imports/Rate-field detection follow later phases.

### P14 saved headers

- Save/load/clear remain scoped to tenant + selected active home client + native template. P14 explicitly grants these actions to tenant administrators and active assigned managers/internees; membership, session/revision, client selection and assignment are rechecked/pinned per operation. This grants no upload, report or cross-tenant sharing access.
- Separate native set/entry tables retain permanent column IDs/keys and literal ERP headers; legacy saved mappings are not adopted or rewritten. Database guards enforce scope/key bindings. Blank choices are omitted; foreign/duplicate column IDs and invalid text are rejected. Comparison policy stays in templates.
- Mutations require current mapping_revision and full configuration_key. Saved-binding signatures exclude labels/annotations but include ordered IDs/keys/types/roles, effective comparison/review policy and template purpose/ledger/rate/subtotal settings. Structural changes flag needs_review and prevent auto-application; label-only changes retain bindings. Clearing retains the set revision to reject stale resurrection.
- Full replacement/clear, scoped validation-plan invalidation and required audit share one transaction. Client attribution uses the audit client field; metadata contains safe tenant/template IDs only. Clearing changes the saved copy, preserving the current grid. Native saved references protect column meaning/deactivation and template deletion even when their HTTP flag is off.
- Shared Upload controls send native revisions only when enabled, reuse exact present headers with verified keys, and discard old client/template/file/sheet/generation replies. Stale or incomplete metadata cannot authorize saves; expired/denied access disables actions. Quoted headers use value equality rather than interpolated selectors. Legacy payloads remain unchanged.
- P14 verifies saved-header behavior with owned PostgreSQL and synthetic staging/auto-map output; workbook reading, complete imports, deployed ownership/bindings, measured schema capacity and live activation remain later gates. No source values, history or financial fingerprints change here.

### P15 configuration backup

- Disabled native export, preview and restore cover existing tenant settings, holidays, columns, templates and saved headers; same-tenant existing identities only. Legacy JSON and transaction restoration remain separate.
- Administrator session/context, preview revision and explicit confirmation are required. Restore validates bindings, retains permanent identities and applies all changes and audit in one transaction; failures roll back. Existing Setup UI remains legacy-only.
- Inactive templates/client mappings, unknown bindings, foreign tenants and unsupported comparison changes require review. Preview identifies affected records and warns that upload backup policy may change. Live activation remains gated.

### P16 staged workbooks

- Disabled local staging binds receipts to tenant, user, selected active home client and native template. Administrators and active assigned managers/internees receive staging actions only; another user's receipt remains inaccessible.
- Caller-generated request IDs reconcile interrupted responses without replay. Reserve and ready transitions require atomic audit; uncertain commits retain receipts/bytes. Staging adds no canonical transactions or validation plans.
- Files stream within configured limits to generated keys outside application trees. SHA-256 is rechecked on status; missing/altered/expired sources require fresh selection. Owner-authorized expiry cleanup commits first, clears discovery and deletes only registered files.
- ExcelDataReader 3.9.0 and the P17 adapter supply bounded sheet/first-row discovery with positional blank/duplicate headers; import controls and frontend integration follow later phases.
- Local storage needs operator-restricted permissions. Durable private storage/retention, unattended cleanup after access revocation, deployed schema/capacity and live activation remain gated; no Render filesystem durability is claimed.

### P17 source-cell boundary

- Internal streaming rows retain physical positions, repeated lines, typed blanks/text/numbers/booleans/dates/times/durations/errors, number formats and formula-cache state; formulas are never evaluated.
- XLSX numeric lexemes survive floating-point conversion; binary XLS exposes round-trip IEEE values and unknown formula presence. Neither representation changes persisted/API number types or approves calculation precision.
- Synthetic XLS/XLSX comparisons record Python source, existing pandas/import coercion and browser differences separately. Missing formula results and invalid numeric values stay explicit; production ERP corpus and full import/accounting parity remain unverified. See CHANGELOG_ARCHIVE.md, Workbook reader P17.
- Configured byte/expanded-byte/sheet/column/row/cell limits, cancellation and disposal bound reading; no canonical writes, new endpoints or frontend activation.

### P18 preparation boundary

- Disabled `POST /api/upload/file/{id}/prepare` supports mapping, date samples and paged source-row choices. Requires owned staging, saved mappings, session/context and CSRF guards; no canonical writes or validation plans.
- Workbook hash/expiry, native bindings and live access are checked around parsing; saved-mapping revision and configuration are rechecked. Responses carry request ID, source/configuration context and an informational selection fingerprint. UI integration must discard obsolete responses; this is not a signed import authorization.
- Exact headers precede ERP aliases and existing fuzzy/positional suggestions. Bind by column ID/key and source position; blank/duplicate source names require explicit positions. Saved names are applied only when unambiguous; comparison policy remains server-owned.
- Date choices retain explicit order/delimiter, native calendar values and Python's bare-serial origin. Samples never confirm coverage; P21 supplies checked transaction-date suggestions. Physical row counts/flags and exclusion/override choices do not certify eligibility; P19 validates them.
- Deadline, concurrency, request/selection/page and response bounds limit work. Current Python Upload UI remains active; live activation, production-corpus/performance proof and import/coverage controls remain later gates. See CHANGELOG_ARCHIVE.md, Upload preparation P18.

### P19 input-validation boundary

- Disabled `POST /api/upload/file/{id}/validate-input` reuses P18's live source/mapping boundary and request limits. Checks write no canonical rows or validation plans; `CanImport` is always false, even when input checks pass.
- Inspect original mapped errors/nonfinite numbers and identifiable missing caches before blank-field decisions. Preserve Python's NA-text decisions separately from raw cells; source faults cannot become force-includable blanks. Optional unmapped Standard fields remain blank; critical mappings are required.
- Preserve numeric syntax, date choices, ledger DR/CR/sign rules, source positions and repeated lines without narrowing stored values or approving calculation precision. Recognize controls/continuation notes without performing Footing, coverage or cross-row ledger balance proofs.
- Partial Standard blank-field exceptions cannot bypass invalid values. Complete reports forbid transaction exclusions/overrides; ledger rejected rows remain blocking. P20/P21 retain financial/scope controls; binary XLS formula presence remains unverified.
- Scan all rows for counts while paging/bounding diagnostic details; truncation never hides blocking status. Request/source/configuration fingerprints are informational. Full import authorization, live UI activation, production corpus and scale acceptance remain later gates. See CHANGELOG_ARCHIVE.md, Input validation P19.

### P20 Footing boundary

- Disabled `POST /api/upload/file/{id}/footing` reuses P18/P19 ownership, freshness, input checks and streaming row classification. Confirm a valid control row explicitly; only mapped Numeric Footing totals determine its numeric suitability. Never infer confirmation from a label.
- Aadil approved bounded BigInteger coefficient/scale arithmetic for Footing only. Parse within Python's thousand-digit/adjusted-exponent bounds; retain exact sums/differences with bounded growth. No source rounding, canonical writes, new dependencies or general report-precision approval.
- Prove full source detail before user exclusions; report selected eligible rows separately. Preserve repeated occurrences/configured document counts and omit controls/notes. Invalid aggregate inputs mark totals incomplete and cannot pass.
- Absolute differences below 0.01 pass; 0.01–1.00 inclusive require current-result acknowledgement; larger differences block. Review fingerprints bind source/configuration/mapping, mode, row choices, policy and totals, excluding request-correlation ID. Changed or inapplicable acknowledgement is rejected; P24 still owns signed import plans.
- Conservatively block amounts that a legacy float conversion could change for precision review. Binary XLS formula presence remains unknown. `CanImport=false` always; P21 supplies coverage checks, while stored SQL proof and live UI/corpus/scale acceptance remain gated. See CHANGELOG_ARCHIVE.md, Footing P20.

### P21 coverage boundary

- Disabled `POST /api/upload/file/{id}/coverage` reuses owned source/mapping freshness, P19 classification and P20 exact Footing in one streaming pass. Source checks and applicable coverage rules must pass; `CanImport=false` always.
- Complete Standard reports require mapped Document Date, canonical ordered ISO dates and explicit full-period confirmation. Every parseable detail date must fit; exclusions/overrides cannot manufacture completeness. Declared boundaries may exceed observed transaction dates; empty replacement requires separate confirmation and genuine absence of detail rows.
- Partial extracts never certify a complete period or authorize removal of absent rows. Preserve existing Standard exceptions and ledger exclusions; included ledger dates must fit their declared period. Complete ledger coverage must match that period.
- Ledger account identity comes from the client's latest parsed Trial Balance posting accounts, checked before/after parsing. Missing, duplicate, unavailable or changed reference evidence blocks; reference identity participates in the review key. No Trial Balance writer/schema change or account-picker UI is included.
- Suggestions scan transaction dates, retaining occurrences and omitting controls/notes. They do not confirm coverage; unresolved source/Footing checks suppress suggested boundaries. P18 date samples remain advisory.
- Review keys bind source/configuration/mapping, mode, dates, account reference, controls, selections and confirmation choices, excluding request-correlation ID and variance acknowledgement itself. Variance acknowledgement also requires the current coverage key. P24 still owns signed plans; stored-row scope/corrections, ledger balance proofs and live activation remain later gates. See CHANGELOG_ARCHIVE.md, Coverage P21.

## 3. PostgreSQL and financial values

| Contract | Required behavior |
|---|---|
| Canonical records | client_data is the sole current transaction set, containing raw JSONB plus report-required derived fields. client_data_history alone holds prior changed/deleted row snapshots. No durable transaction mirrors. |
| Scope/keys | Preserve existing IDs, client/template/account scope, source identities/fingerprints, creation time, and source positions. Account codes remain text; preserve leading zeros and configured case semantics. |
| Raw values | Preserve row_data value types and source precision. Existing parsing/formatting rules remain the oracle; do not stringify every public/database number or rewrite stored rows for convenience. |
| Exact comparison | Numeric encodings such as 200 and 200.000 compare by value; blank differs from zero. Reject nonfinite/error inputs under existing rules. Preserve separate raw/concurrency fingerprints and comparison fingerprints. |
| Calculation precision | Use typed exact amounts internally and lossless serialization where persisted/transferred. Python statements use 50-digit Decimal precision; C# decimal has 28–29 significant digits. Prove supported ranges, scale, intermediates, overflow and rounding before each numeric port. If insufficient, approve a lossless alternative; never silently narrow accepted inputs or substitute double. Preserve calculation policy and configured presentation rounding. |
| Existing approximate helpers | database.py declares extract_amount REAL and derived_debit/derived_credit DOUBLE PRECISION. Do not silently change their types, backfill them, or treat an approximate helper as an exact source amount. Precision repair remains separately scoped. |
| Dates/time | Business dates use date-only values and ISO YYYY-MM-DD internally; timestamps are UTC instants. Preserve raw source date interpretation, display formats, and null/empty defaults; never shift a business date through local timezone conversion. |
| Identity serialization | Port _canonical_source_identity/fingerprint rules with byte-exact hash vectors, including Unicode escaping, field order, numeric formatting, and repeated occurrences. Preserve existing identities; a new algorithm needs an explicit version/migration. |
| Upload policy | Unchanged uploads are no-ops; legitimate repeated posting lines survive. Complete coverage may remove missing rows with history; partial/correction extracts retain absent rows and reject ambiguous pairing. Empty-period clearing requires its existing confirmation. |
| Source controls | Preserve mapped critical fields, confirmed control rows, and exact absolute Footing differences: below 0.01 passes; 0.01 through 1.00 needs bound acknowledgement; above 1.00 blocks. Optional Standard fields follow current template policy. |
| Validation plans | Preserve one ready plan per client/template, the current 30-minute expiry, file/request/configuration hashes, server-authoritative comparison settings, and concurrency locks. skip_validation cannot bypass mandatory controls. |
| Atomicity | Apply current rows, history, counts, required derived changes, and completion controls in one transaction. Preserve savepoint isolation where verification is diagnostic. Lost commit acknowledgement requires status reconciliation. |
| Report reads | Use one read-only consistent snapshot for related inputs. A whole read may restart once with all partial results discarded; never borrow a second independent connection inside that snapshot. |
| Revision/caches | Cache identity includes client, dates, accounts, report options, configuration, and source freshness. Preserve hard refresh, bounded retention, stale-response rejection, and complete-scope totals. |
| Deletion | Protected administrator purge uses fresh preview/confirmation and checks Verification/evidence. Preserve other clients, settings, mappings, Trial Balance, and required indexes. Download initiation does not prove the user saved a backup. |
| Schema ownership | Review one additive migration owner per change; neither runtime independently upgrades shared tables. Inventory actual schema before P04 changes; 95 source-declared names do not prove live presence or justify dropping legacy tables. |
| Audit preservation | .NET records the authenticated actor and preserves audit/error history. Plan the remaining SQLite records' export/reconciliation before removing SQLite-dependent production paths. |

Sources: [database schema/helpers](../database.py), [upload comparison/import](../routers/import_export.py), [accounting conventions](../CLAUDE.md). Existing stored shapes and exact response fixtures take precedence over an incidental .NET serialization default.

## 4. Capacity decision C01 — 500 MB

Aadil chose the Free-plan 500 MB ceiling. Supabase currently lists 500 MB database size per Free project; database quota and physical disk/WAL pressure are separate concepts. Its current disk documentation does not describe 500 MB as the automatic read-only trigger. Sources checked 2026-11-10: [billing quotas](https://supabase.com/docs/guides/platform/billing-on-supabase), [database/disk size](https://supabase.com/docs/guides/platform/database-size).

- Target application ceiling: 500,000,000 bytes (decimal MB). Reconcile the application's measurement/display with provider metering before release; label MiB separately.
- Replace the fixed 400 MB rejection policy with a projected-total check against 500 MB. Include additional transaction/history/index/validation/audit growth and measured uncertainty headroom; do not interpret the ceiling as 500 MB of new workbook data.
- Serialize competing capacity reservations/rechecks; do not let simultaneous jobs each spend the same free space. Refuse work whose projected peak reaches/exceeds the ceiling; treat unavailable capacity measurements as unverified, not unlimited.
- Deletes do not imply reclaimed allocation; measure reusable/allocated space and temporary peaks. Do not introduce automatic compaction or a paid plan.
- The existing 300 MB warning is informational, not a rejection limit. Its presentation can be reviewed with capacity UI; no fixed 400 MB stop remains in the target policy.
- Root CLAUDE.md and Python budget helpers still contain 400 MB, and the pending import_export.py patch removes calls. This phase changes none of them; align both active writers with C01 in the approved P04/P24–P29 work before their cutover. P03's local foundation is not blocked by that implementation.

## 5. C# report ownership

All report layers move to C#. Check callers and runtime reachability before porting mixed files; preserve proven SQL and accounting semantics. P01's Python-retention proposals are superseded, while its captured code remains the comparison baseline.

| Existing source | C# responsibility / acceptance |
|---|---|
| routers/reconciliation.py | Configuration, authorized queries, report APIs, previews/drilldowns, diagnostics and download coordination; preserve wire shapes and source-bound confirmations. |
| ledger_report_source.py | Canonical SQL, scoped source selection, normalization, Treasury projections, revisions and invalidation; never reinstate retired durable transaction copies. |
| financial_statement_balances.py | Statements, Notes, account hierarchy, current/comparative balances, signs, subtotals, profit linking and transfer controls; remove router-helper coupling in the C# design. |
| ledger_group_summary.py | Cashflows and group summaries, periods, signed reversals, account selections, transfers, party allocations and net/gross controls; pandas operations become scoped SQL or typed C# algorithms. |
| reconciliation_balance_source.py | Independent balances, posting comparisons, adjustments, voucher completeness and supporting evidence; preserve unavailable/empty distinctions. |
| reconciliation_xlsx.py | Financial workbook layouts, literal-text safety, complete evidence, styles and bounded writing; compare cells/totals and peak memory before choosing an approved .NET writer. |
| reconciliation_export_jobs.py | Durable job ownership, scheduling, progress, cancellation, recovery, expiry and private artifacts; local files/OS locks alone do not coordinate hosts. |

Upload controls, Verification status gates and operational exports move with their modules. Future vendor/customer, expense and sales quantitative reports also default to C# but require separate feature briefs. AI/statistical integration is deferred until a concrete need justifies it; no Python runtime is required by the target design.

## 6. Internal report inputs, results and jobs

The browser calls .NET; authorized query services supply typed inputs to C# calculation/export services. Keep these boundaries inside the modular application; no cross-language HTTP protocol, generic dataframe replacement or JSON round-trip is required. Version persisted job/artifact formats when needed, not every in-process call.

| Field group | Contract |
|---|---|
| Identity | Bind report kind, calculation/configuration version and source identity; jobs/artifacts carry an opaque ID and input digest. Reject unsupported persisted formats. |
| Authorized scope | Client, template/TB upload, period bounds, grouping, Treasury view, and explicit account-selection mode. Omitted legacy All follows existing behavior; an explicit empty/incompatible selection never broadens to All. |
| Source identity | Source upload/record revision, selected TB, configuration/context fingerprints, and UTC capture time. All constituent data comes from the same database snapshot. |
| Input datasets | Load only the journal/account/category/assignment/calculation/balance/evidence sets required by the report. Preserve completeness, stable ordering and counts; missing differs from complete-empty. Persisted artifacts additionally need schema/checksum validation. |
| Exact values | Use the approved numeric policy, date-only business values and UTC instants. Raw row_data retains original types/values; public DTO conversions preserve existing wire shapes. |
| Bounded execution | Aggregate/filter in SQL where semantics permit; use bounded C# structures and streaming/private temporary artifacts for large work. Measure end-to-end memory/time, including workbook packaging. Never truncate evidence to meet limits. |
| Snapshot lifetime | Capture complete inputs while holding one read snapshot, then close it before long calculation/export. Never assemble related pages from changing independent snapshots. |
| Result | Bind totals, sections, controls and diagnostics to the captured source/configuration/options. Preserve public report/download contracts and reject substituted scope or incomplete input. |
| Artifact | Record private locator, content type, byte length, checksum, owner/scope, source identity, and expiry. Financial files remain temporary; job metadata never becomes another transaction store. |
| Failure | Use approved public errors and bounded safe diagnostics; no successful report from incomplete inputs. Preserve prior accepted results only with truthful source/freshness labels. |

Source/configuration freshness is part of every result and cache key. Preserve each endpoint's existing current-versus-as-of behavior; never describe mixed snapshots as current. Require an explicit recompute or conflict result when freshness checks fail.

Proposed job states are queued, preparing_inputs, running, succeeded, failed, cancelled and expired; adapt legacy public statuses separately. Persist owner/lease/attempt/progress, bound retries over immutable inputs and handle worker loss. Calculation/export code does not mutate accounting records. Final job/storage limits require P28/P56/P62 approval; heavy export activation waits for P56 recovery/access checks.

## 7. Dependencies and rollback

| Phase band | Entry condition and compatibility bridge |
|---|---|
| P03–P06 | Retain accepted foundations. Shared-schema execution still needs catalog/capacity evidence, ownership and rollback proof. P06 proves explicit legacy/.NET routing with stubs; no Python report-service client. |
| P07A–P07C, before P08 | Agree foundational direction, then separately approve and rehearse tenant and sharing foundations; resolve detailed policies in relevant phases before enabling operations. Preserve P07 acceptance and existing data; no public switch before proven cross-application authorization. |
| P08–P15 | Tenant membership, engagement assignment, sharing and session/client/role behavior must be consistent across both applications before route switches. Capture response fixtures and verify configuration guards. |
| P16–P29 | Valid client/template/mapping inputs and shared transactions are prerequisites. Settle capacity implementation, hash vectors, coverage/Footing policy, and source revision invalidation before enabling imports. |
| P30–P40 | Preserve existing Verification/evidence references during purge and both consumers during setup changes. P37 brings forward minimum statement/context calculation for profit confirmation; P38 preserves classification/preparation/revision effects; P39 retains vendor workstream assignments read by Cashflows. |
| P53A–P56, after P40 | Execute eight report steps in the plan: inputs/precision, statements, Notes, Cashflows, reconciliation/evidence, APIs/screens, workbooks and jobs/acceptance. Reuse P37's proven helper; compare source controls, legacy results and exported evidence before switching each operation. |
| P41–P45, after reports | Port Audit Rules documents, cross-checks, categories, compilation and historical guards. Keep report categories independent of Verification classifications and preserve shared metadata/revisions. |
| P46–P52 | Rulebook/assignment contracts and evidence IDs must already match. Test frozen histories, concurrent edits, and shared-modal behavior before switching writes. |
| P57–P63 | Reconcile audit/Drive/backup dependencies, full acceptance evidence, deployment/storage limits, and rollback rehearsal before retiring old modules. |

IDs remain stable; execution is P06 → P07 → P07A–P07C → P08–P40 → P53A–P56 → P41–P52 → P57–P63. During coexistence, legacy endpoints retain their existing Python execution/database path until accepted replacements take ownership. P06 is a migration bridge, not a permanent reporting dependency. Pull forward a bounded prerequisite or retain the whole legacy operation when a later dependency blocks safe switching; never introduce an unapproved Python helper service.

Each operation/scope has one active writer. Shadow comparisons use isolated data, not duplicate production imports. Keep shared schemas backward-compatible; reconcile uncertain commits before routing back, never fail over by replaying a write. Rollback never deletes history, drops required columns or overwrites newer unrelated work. Destructive retirement needs protected backups and explicit acceptance/authorization.

## 8. Acceptance fixtures to establish

Expected results come from independent source controls plus the captured existing engine/SQL behavior; unresolved baseline defects are recorded rather than copied or silently fixed. Use synthetic fixtures and approved external workbook copies; never commit client files. These are planned checks, not results already run.

| Set | Required comparisons |
|---|---|
| G01 — API/access | Exact API shapes; expired/inactive sessions, both tenant types, membership, assigned/unassigned staff, configurable sharing/CEO visibility, policy changes, revocation and foreign object IDs. |
| G02 — Cells/amounts | XLS/XLSX native/text/serial dates, formulas/errors, Unicode, leading-zero codes, exact rates, trailing-zero equivalence, blank versus zero, nonfinite input, and row order. |
| G03 — Source controls | Footing differences below/at/above 0.01 and 1.00, confirmed control rows, full coverage, optional fields, rejected exclusions, and empty-period confirmation. |
| G04 — Identity/history | Original/repeated/renamed/reordered inputs; explicit IDs, identical repeated lines, unique corrections, ambiguity, full/partial removal, exact history and receipt counts. |
| G05 — Atomicity/capacity | Expired/altered plans, simultaneous imports/reservations, real rollback, lost commit acknowledgement, and projected peaks just below/at/above 500 MB. |
| G06 — Trial Balance/setup | Hierarchy and code corrections, period overwrite, mappings, independent report/Verification classifications, profit context, and missing balance evidence. |
| G07 — Rules/Verification | Rule compilation/versioning, all status gates, evidence/Forms/procedures/observations, save/verify concurrency, session ranges, and historical result protection. |
| G08 — Financial reports | C# versus independent source controls and legacy Cashflows/Treasury views, selections, periods, gross sides/adjustments, statements/Notes, evidence and workbook cells; include numeric bounds, rounding/overflow, large amounts and repeated fractions. |
| G09 — Jobs/freshness | Authorized tenant/engagement scope and revocation across caches/jobs/downloads; source/config changes, corrupt artifacts, restart, cancellation, expiry and concurrency. |
| G10 — Operations | Current/history exports, guarded purge, restored schema/data/security/sequence checks, credential expiry/reconnect, and legacy URL compatibility. |

P03 establishes the isolated .NET harness; each port phase adds its relevant fixtures, with P61 cross-module acceptance. Existing Python test functions and archived pass counts identify coverage, not a current executable baseline. Performance targets require source-backed timings in comparable environments.

## 9. Acceptance and remaining verification

- C01's 500 MB ceiling is decided; capacity code/helper changes are deferred to their approved phases. No quota, schema, Python source, or live service was changed here.
- Aadil approved this ownership/sequence revision; completed implementation remains accepted. Subsequent implementation, local acceptance and Git steps retain their separate approvals.
- Deployed schema/provider metering remain unverified; local P04 tests do not certify them. Per-route fixtures, authorization, hash vectors, numeric bounds, golden outputs and legacy reachability remain phase work.
- Review C# report ownership, execution order, prerequisite guards, API/value preservation and capacity policy. No local-server test is needed for this documentation revision; implementation phases require their own checks and acceptance.
