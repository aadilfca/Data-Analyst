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
- Administrator session/context, preview revision and explicit confirmation are required. Restore validates bindings, retains permanent identities and applies changes/audit atomically. P29 adds native Setup controls; Python retains its transfer flow.
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
- Bounded BigInteger coefficient/scale arithmetic covers Footing, Trial Balance validation, source/storage comparisons and saved footing controls. Preserve bounds; no rounding, type widening or general report-precision approval.
- Prove full source detail before user exclusions; report selected eligible rows separately. Preserve repeated occurrences/configured document counts and omit controls/notes. Invalid aggregate inputs mark totals incomplete and cannot pass.
- Absolute differences below 0.01 pass; 0.01–1.00 inclusive require current-result acknowledgement; larger differences block. Review fingerprints bind source/configuration/mapping, mode, row choices, policy and totals, excluding request-correlation ID. Changed or inapplicable acknowledgement is rejected; P24 still owns signed import plans.
- Conservatively block amounts that a legacy float conversion could change for precision review. Binary XLS formula presence remains unknown. `CanImport=false` always; P21 supplies coverage checks, while stored SQL proof and live UI/corpus/scale acceptance remain gated. See CHANGELOG_ARCHIVE.md, Footing P20.

### P21 coverage boundary

- Disabled `POST /api/upload/file/{id}/coverage` reuses owned source/mapping freshness, P19 classification and P20 exact Footing in one streaming pass. Source checks and applicable coverage rules must pass; `CanImport=false` always.
- Complete Standard reports require mapped Document Date, canonical ordered ISO dates and explicit full-period confirmation. Every parseable detail date must fit; exclusions/overrides cannot manufacture completeness. Declared boundaries may exceed observed transaction dates; empty replacement requires separate confirmation and genuine absence of detail rows.
- Partial extracts never certify a complete period or authorize removal of absent rows. Preserve existing Standard exceptions and ledger exclusions; included ledger dates must fit their declared period. Complete ledger coverage must match that period.
- Ledger account identity comes from the client's latest parsed Trial Balance posting accounts, checked before/after parsing. Missing, duplicate, unavailable or changed reference evidence blocks; reference identity participates in the review key. No Trial Balance writer/schema change or account-picker UI is included.
- Suggestions scan transaction dates, retaining occurrences and omitting controls/notes. They do not confirm coverage; unresolved source/Footing checks suppress suggested boundaries. P18 date samples remain advisory.
- Review keys bind source/configuration/mapping, mode, dates, account reference, controls, selections and confirmation choices, excluding request-correlation ID and variance acknowledgement itself. Variance acknowledgement also requires the current coverage key. P22 adds read-only stored identity checks; P23 corrections, P24 plans, ledger balance proofs and live activation remain gated. See CHANGELOG_ARCHIVE.md, Coverage P21.

### P22 identity boundary

- Disabled `POST /api/upload/file/{id}/identity` shares P19–P21's classified source pass and surrounding ownership/configuration/reference checks. Read current rows through bounded keyset pages in one enforced read-only snapshot; retries discard partial attempts. No canonical/history/audit/plan writes, schema changes, persistent transaction copies or new packages.
- Match each occurrence once, reserving exact matches before formatting equivalents. Numeric comparison retains exact values and blank/zero distinctions; Text identifiers retain leading zeros. Known calendar dates govern comparison; ambiguous stored text is not guessed. Description spacing equivalence requires a verified semantic binding; ledger notes remain associated with their original parent.
- New identity proposals preserve Python v2 escaping/order/numeric formatting and occurrence rules; matched rows retain stored identities. Raw JSON guards and comparison views remain separate. Duplicate verified Line IDs, active identity collisions (including outside the preview period), missing matched identities and inconsistent active/deleted metadata cannot yield a passing no-op.
- Native metadata still lacks immutable Line ID/derived-Rate bindings: return review-required instead of discovering these from editable captions. Also review blank-document fallback/coercion, non-ASCII Line-ID folding, unsupported stored structures and numeric identities exceeding 28 significant coefficient digits after trailing-zero removal. These are compatibility gates, not source edits or a new hash/rounding policy; imported Rates are never rounded.
- Report transaction matches separately from a full no-op. Unchecked-field/source-order changes prevent a full no-op; ledger balance/coverage evidence is not certified by unchanged transaction rows. Unmatched rows pass to P23 review. Existing identity-index/catalog verification remains a writer/cutover prerequisite.
- Bind review keys to coverage/source/configuration, profile, captured store values/versions/order and matches; reject stale keys. `CanImport=false` always, and no-op evidence describes its captured snapshot rather than a committed import or signed plan. Retained rows/bytes, individual serialization, diagnostics, responses, deadlines and concurrency are bounded. See CHANGELOG_ARCHIVE.md, Identity P22.

### P23 correction boundary

- Disabled `POST /api/upload/file/{id}/corrections` consumes P22's checked source/snapshot and shared exact-first occurrence matcher. No canonical/history/audit/plan writes, new schema, packages, durable transaction copies or UI activation; `CanImport=false` always.
- Compare within a document, consuming each stored occurrence once. Preserve existing IDs/identities/versions for safe corrections; distinct explicit Line IDs never inherit another line's history. Numeric comparisons retain exact values and blank/zero distinctions.
- After unchanged matches, use unique non-Numeric field anchors. Trusted profiles also support authoritative Line IDs, unique account/movement-side anchors and an explicitly permitted single-row fallback. Never sort mutable amounts or use source position as identity.
- Native metadata still lacks immutable fallback account/side and Line ID bindings: unresolved two-sided native comparisons require review rather than editable-caption inference. P22's derived-Rate/coercion/precision gates remain. Pure verified-profile parity does not certify native metadata or live activation.
- Partial extracts retain absent rows and block ambiguous correction candidates. Complete reports may classify unpaired additions/removals only with passing coverage and identity checks; ambiguity never becomes a guessed Changed pairing. Return changed field keys, separate informational/source-order counts, and bounded paged actions.
- Read whole-voucher membership in the same enforced read-only snapshot. Changed/removed Standard vouchers must fit the captured scope; ledger incoming vouchers cannot extend beyond the selected account/period. Other clients/templates/accounts and unrelated outside vouchers remain excluded. Recheck proposed identity collisions after correction pairing.
- Review keys bind coverage, captured values/versions/order, membership, policy, identities and all actions independently of pagination. Reject stale evidence; counts/status remain authoritative when diagnostic details are bounded. Concurrency, deadlines/cancellation, rows, retained bytes, body and response sizes are limited.
- Results describe the captured snapshot, not a committed import. P24 signed plans, P25 atomic writes/history, ledger balance proofs, binding/index review and full Upload acceptance remain separate gates. See CHANGELOG_ARCHIVE.md, Corrections P23.

### P24 validation-plan boundary

- Disabled `POST /api/upload/file/{id}/validation-plan` and `/validation-plan/check` use complete P23 evidence, independent of preview pagination. Persist bounded actionable values, validated dates and current-row guards; unchanged rows are not copied. Metadata-only actions contain only unchecked-field updates and source positions, never replacement financial values.
- Keep payloads in `upload_validation_plans`, with a versioned native envelope/retry receipt in `platform_upload_validation_plans`. Existing template/column/mapping invalidation remains authoritative. Candidate `0012` enforces one ready plan per client/template and rejects legacy conflicts without repair/adoption; startup runs no DDL.
- HMAC-SHA256 binds owner/tenant/client/template/file, request/coverage/configuration, mapping revision, captured data/actions, payload hash and expiry. Require a stable shared 32–64-byte base64 `UploadValidationPlans__SigningKey` when enabled; keep it outside Git/logs. Rotation invalidates existing signatures. Native and legacy request signatures are distinct protocols; never route native plans into Python import.
- Plans expire after 30 minutes or staged-file expiry, whichever comes first. New validation supersedes the previous ready plan and discards its payload. A true no-op creates no action plan; it retires old ready plans and keeps a compact retry receipt. Receipts survive payload retirement for 24 hours; cleanup and retention are bounded per scope, default 128 receipts.
- Repeating a request ID with identical fresh evidence returns the same result/expiry; changed reuse, expired/superseded plans and cross-owner scope are rejected. Lost COMMIT acknowledgement is reconciled by receipt lookup without write replay; an unresolved outcome requires retrying the same request ID. File, configuration, permissions and full current-row/voucher guards are rechecked; workbook parsing holds no write lock.
- Store/read the lossless serialized payload as a JSONB string value so PostgreSQL cannot normalize embedded number tokens. Verify its digest and envelope signature before use, plus common-table identity/status/expiry. Do not trust client-supplied actions or skip-validation flags.
- Limit requests to 1 MiB, plan payloads to 4 MiB, rows to 100,000 and source comparisons to existing bounds; enforce concurrency, deadlines/cancellation, scoped receipt limits and bounded cleanup. Plan writes serialize a conservative database-size check including payload/index headroom under the lower configured plan/platform ceiling. Provider metering and all-writer capacity coordination remain cutover gates, not certified by local fixtures.
- Write only temporary plan metadata; no canonical/history mutation or live UI activation. Existing identity/binding/precision gates remain; ledger plans require their unported balance proof. `CanImport=false` always. P25 must repeat final plan/status/current-data checks inside its atomic write transaction. See CHANGELOG_ARCHIVE.md, Validation plans P24.

### P25 atomic-import boundary

- `UploadImports__Enabled=false` gates `POST /api/upload/file/{id}/atomic-import`; enabling requires P24 and its prerequisites. This isolated foundation does not activate the existing Upload UI; P27 adds completion evidence.
- Consume only a current signed native plan. Recheck live owner/tenant/client/assignment, source bytes, configuration/mapping, expiry, current-row guards, full-voucher membership and the required active-identity index under shared writer locks.
- One supplied transaction writes current rows, exact prior changed/deleted history plus source/date metadata, counts, audit, Imported status, plan consumption and a compact recovery outcome. Corrections preserve IDs/creation time and return linked audit reviews to Pending; information-only changes merge unchecked fields/source positions without financial history. Complete removals retain history and block protected references; partial extracts retain absent rows.
- Candidate `0013` adds nullable history metadata and `platform_upload_imports`, never a durable transaction mirror. Same-plan deliveries return one owner-bound outcome; operation-ID reuse with different input rejects. Recovery checks access before source/plan availability; uncertain COMMIT never replays writes and unresolved outcomes require the same operation ID.
- Batch writes, strict 1 MiB bodies, deadlines/cancellation, concurrency and conservative projected database headroom remain bounded. P26 derivation guards and P27 completion controls apply; ledger proofs and retired transaction-copy dependencies remain gated. Shared-schema execution, provider capacity evidence, cleanup integration, source corpus/scale and P29 UI acceptance remain cutover gates. See CHANGELOG_ARCHIVE.md, Atomic import P25.

### P26 derivation boundary

- Native template `uploadBindings` map semantic roles to active selected column IDs, separate from labels and reconciliation roles. Omitted edits preserve them; rebinding with current/history data or uploads requires migration review. Rate cannot overwrite its input column; debit and credit remain distinct. Candidate `0014` adds guarded scoped metadata and a nullable `derived_configuration_hash`, with no automatic legacy adoption, backfill or type changes.
- Resolve automatic Rate source from this upload's mapping. Preserve imported scalar values/types exactly; derive before identity/correction comparison using tenant precision 0/2/4. Bounded exact arithmetic must agree with established binary64 rounding at that precision; disagreements, unsupported numbers and invalid settings require review. Missing/zero Quantity leaves derived Rate unavailable.
- Keep report fields alongside raw JSON only in `client_data`: dates/month/fortnight, account/name, debit/credit, Treasury classification and approximate `extract_amount`. Preserve the explicit Amount preference/nonzero numeric fallback; existing REAL/DOUBLE PRECISION helpers never become exact source totals. Configuration keys include permanent bindings; signed plans additionally bind relevant settings, Treasury/source mappings, captured freshness markers and incoming voucher counts.
- Derivations, compact voucher decisions, actual report-revision increments and cache invalidation share P25's transaction/recovery receipt. Metadata refresh creates no financial history. Partial uploads retain absent vouchers; incomplete touched vouchers or stale retained derivations require review. Complete removal retires compact current decisions without erasing audit history. Enforce bounded batches/capacity, whole-voucher guards and unchanged IDs/evidence.
- Import flags remain disabled. ASCII case normalization is currently proven for compact voucher identities; unsupported case/precision cases stop for review. Ledger balance proofs, retired transaction-copy dependencies, deployed catalog/capacity evidence, configuration backup/UI integration and full source-corpus/scale acceptance remain gates. P27 adds completion evidence; P28 jobs and P29 Upload UI remain separate. See CHANGELOG_ARCHIVE.md, Derived fields P26.

### P27 completion-evidence boundary

- `UploadImports__Enabled=false` also gates owner-scoped `GET /api/upload/file/{file}/receipt/{id}`, `/workbook` and `POST /receipt/check`. Existing session, active tenant/client/assignment, context and CSRF rules apply; historical access does not require an unexpired source or current template configuration.
- Signed plans bind full-sheet and approved-detail controls separately, Footing IDs/keys/labels, footer acknowledgement, declared dates and incoming row references. Old actionable plans without source evidence require revalidation. Independent SQL counts/documents/exact raw-JSON totals run in the import transaction; approximate helpers never certify amounts. Partial extracts remain `not_comparable`; missing IDs, differences and unavailable controls are explicit qualifications.
- Diagnostic SELECT errors roll back to a savepoint; cancellation, broken connections, failed rollback and commit uncertainty propagate to the owning transaction handler. A committed import can carry `mismatch` or `unavailable`; write/identity/count failures still roll back. Technical reconciliation is not manager-reviewed audit approval or a claim of current freshness.
- Candidate `0015` stores immutable compact receipts atomically with the native outcome: controls, frozen column metadata, counts, group digests and integer row references, with no raw transaction copies. Same-operation retries recover original evidence. A fresh unchanged-data check uses a new request ID and live guards without updating transaction rows; same-ID retries recover the original check. Ledger checks await their separate proof.
- Framework-only OOXML exports include summary, reconciliation, issues, previous full voucher context and selected incoming lines as actually stored at completion. Current rows plus this operation's history reconstruct evidence; digest mismatch, expiry, invalid cells or size limits explicitly refuse detail export. No silent truncation or reconstruction from newer data. Preserve safe literal text, leading zeroes, dates and long/tiny exact amounts.
- Defaults: 128 KiB receipt, 8 MiB XLSX, 10,000 combined evidence rows, two concurrent requests and 60 seconds. Detail expires at source expiry or two hours; unchanged checks cap at 128 per owner/file. Capacity includes receipt/index headroom. Jobs follow P28; UI, scoped purge/retention, deployed capacity and corpus/scale acceptance remain gated. See CHANGELOG_ARCHIVE.md, Import receipts P27.

### P28 durable upload jobs

- `UploadJobs__Enabled=false` gates `POST /api/upload/file/{file}/jobs`, `GET /jobs/{id}` and `POST /jobs/{id}/cancel` or `/resume`. Version-1 requests select `import` with a signed plan or `no_changes`; request ID becomes the operation/check ID. Owner/session/context/tenant/client/assignment checks apply. Identical request/plan deliveries converge; changed reuse rejects.
- Candidate `0016` retains immutable scope, bounded review options/digests, source references and compact completion, never transaction-row copies or bearer credentials. Workers resolve the explicitly attached session by its hash. Expired/revoked/changed context pauses work; owner resume reauthorizes without renewing inputs or resetting attempts.
- PostgreSQL claims, leases and increasing ownership versions coordinate hosts. The atomic transaction locks/checks job ownership and commits success with accounting/outcome/receipt. Cancellation wins before that lock; a pinned transaction reports finishing/busy. Worker shutdown rolls back or requires outcome recovery. Never undo committed imports or renew stale plans automatically.
- Recover existing outcomes before execution, expiry or retry exhaustion. Unresolved commit uncertainty stays recovering; another attempt retains the same operation and rechecks its durable outcome under existing writer locks. Only transient failures retry; changed/superseded/missing inputs require review. Progress exposes stages/attempts and original committed results, without speculative row percentages.
- Private Supabase Storage HTTP uses immutable generated keys, a bound endpoint/bucket hash, byte/hash read-back and bounded transfers. `StorageUrl`/`StorageBucket` are non-secret settings; `UploadJobs__StorageKey` is server-secret only. Source-ready means verified private persistence; earlier interruption can require fresh selection. Missing local sources can be restored; altered bytes block. A separate cleanup loop deletes registered objects/restore fragments after expiry/terminal transfer grace, preserving accounting evidence. Changing storage location requires reconciling registered objects first.
- Defaults: two global workers, eight outstanding jobs per owner/32 overall, three attempts with 2/10-second waits, 120-second leases/15-second heartbeat, 120-second transfers, 64 MiB/file and 256 MiB reserved object bytes. Preserve plan/source expiry; retain up to 128 jobs per owner/client/template with bounded 24-hour terminal cleanup after confirmed source deletion. Submit bodies cap at 1 MiB/two readers; cancellation/resume bodies at 1 KiB; short actions at 30 seconds. Database metadata reserves headroom below C01.
- Verification uses owned PostgreSQL and local HTTP storage only. Live private-bucket policy, credentials, provider file/quota/egress limits, deletion behaviour, representative large transfers and hosting costs require deployment approval; no bucket is provisioned or live migration run. Existing P16 staging retention, P29 UI and P33 scoped purge remain separate gates. See CHANGELOG_ARCHIVE.md, Durable upload jobs P28.

### P29 configuration integration

- Native template APIs expose `upload_bindings` with permanent selected column IDs. The editor offers compatible accounting fields without caption-based inference; labels/order preserve assignments. Omission preserves existing bindings; explicit empty bindings request guarded clearing. Ordinary edits and backup preview/restore share P26's schema/data/upload guards. Ledger proofs and upload execution remain gated.
- Version-2 configuration exports record every template's explicit binding set, including empty sets. Restore applies these assignments with existing settings/columns/templates/mappings/holidays and required audit in one transaction. Version-1 files with recorded bindings remain supported; missing bindings require review when the current template is bound, rather than silently preserving or clearing them.
- Shell capability metadata/context headers control native Setup and extend its existing navigation registry. Native disabled state cannot fall through into legacy transfer/full-backup routes. Python payloads and full-backup flow remain compatible. Default flags stay off; no schema, dependency or live-route activation is added.
- Native export/files cap at 7 MiB within the existing 8 MiB restore-body limit. The browser retains exact original JSON text, uses parsed values only for display, and requires server preview plus explicit confirmation. Changed files, stale previews and failed/uncertain writes invalidate confirmation; never automatically repeat a restore. Server revisions/authorization remain authoritative; browser requests have 30-second bounds.
- Configuration verification uses owned PostgreSQL, real Kestrel and existing-venv Chromium with synthetic configuration only. See CHANGELOG_ARCHIVE.md, Upload configuration P29A; execution verification follows below.

- P29B's `UploadUi__Enabled=false` is separate from API rehearsal flags; enabling requires shell/jobs and their prerequisites. Native hosting selects its own coordinator even when disabled, with no legacy upload fallback. Package only the existing Excel asset; startup adds no DDL or dependency.
- Native source previews use bounded server pages and string-valued financial tokens. Source/configuration/mapping, date, row, control or coverage changes invalidate review. Imports require explicit count/scope confirmation plus a fresh signed-plan check; true no-ops use the unchanged-data job. Existing ledger/precision/dependency gates remain authoritative.
- Persist compact file/job handles before submission, never source rows or credentials. The request UUID is the job ID; lost responses/refresh use owner-checked status. Writes never retry automatically; explicit same-request recovery preserves its immutable ID/options. Progress reports actual stages/attempts/source readiness, with bounded polling, cancellation and explicit owner resume; expiry is never renewed.
- Completion counts and exact SQL-control strings come from the immutable receipt. Detail downloads remain server-bounded and can expire/refuse stale evidence. Synthetic original/repeat/correction/partial/empty, 1,000-row import and 10,000-row bounded-refusal checks pass; locally accepted. Representative corpus/scale and deployment activation remain gates. See CHANGELOG_ARCHIVE.md, Upload UI P29B.

### P30 Data Bank boundary

- `DataBank__Enabled=false` requires PostgreSQL, shell, tenant templates and settings when enabled. Native pages/APIs never fall through to Python; startup adds no DDL. Default bounds: two readers, 30 seconds, 200 rows/page, 1 MiB responses, 500 templates, 1,024 columns/template and 10,000 ledger accounts; oversize results refuse explicitly.
- P30 grants home-workspace Data Bank reads to tenant administrators and active assigned managers/internees only. Revalidate session, membership, selected tenant/revision/client and assignment within each operation and again before release. Global roles, client selection, team eligibility and browser caches grant no additional access; sharing remains gated.
- Capture configuration, filters, page and exact controls in an enforced read-only repeatable snapshot. Availability may omit slower metrics; metrics failure leaves the list usable. Calendar holidays use the selected tenant; overdue retains business-day frequency rules. Ledger choices use imported metadata, exact account codes and original source order.
- Preserve raw JSON value types and repeated lines. Project permanent storage keys to current labels; native numeric-text companions avoid browser precision loss. Footing aggregates use PostgreSQL NUMERIC over raw JSON; invalid/unsupported totals remain unavailable. Existing approximate amount filters retain their semantics. Full-selection controls are independent of paging/subtotals.
- Retained pages guard complete scopes, clear obsolete values, bound caches and reject late/wrong-context responses; native subtotals use exact bounded coefficients. Template Viewer remains a marked 200-row view with search within that page. Native history, exports, deletion, frequency editing, Cashflow classification and Verification field-policy previews await their own phases.
- Verification uses owned PostgreSQL and synthetic Python/browser comparisons. Representative corpus/scale, shared-schema/catalog compatibility and explicit live activation remain gates. See CHANGELOG_ARCHIVE.md, Data Bank P30.

### P31 history/export boundary

- `DataBank:HistoryEnabled=false` and `DataBankExports:Enabled=false` require P30 reads. History/matching exports explicitly permit P30's home administrators and active assigned managers/internees; complete current/history backups require tenant administrators. Context, object access and revocation are rechecked before release; sharing and purge remain gated.
- History preserves prior JSON, saved action/fields, actor/time and exact ledger scope, including history-only accounts/templates. Unique legacy delete/reinsert matches affect display/action filters only. Saved upload changes and legacy audit records have separate counts/paging; unknown prior field detail stays explicit. Labels resolve permanent keys; exact-number companions preserve browser values.
- Matching downloads use the loaded preview's dates, filters and account; dirty selections require reload. Complete backups ignore preview filters and include all dates/accounts on Data and Changed-Deleted History sheets. Preserve every raw field, source order, line multiplicity, original action, changed-field JSON and source metadata; source-row JSON also preserves null/missing/type distinctions.
- Capture complete inputs in one enforced read-only snapshot, close it before ZIP/XML work, then recheck access. Default export bounds: one reader, 60 seconds, 100,000 combined rows, 32 MiB input, 8 MiB compressed/64 MiB expanded workbook; oversized inputs/cells refuse without partial files. No persistent artifact, schema or dependency is added; heavy export/job activation remains gated.
- OOXML writes literal text/leading-zero codes, booleans and exact numeric tokens. Values outside Excel's reliable numeric precision/range remain text. Escape literal `_xHHHH_`, reject ambiguous headers/unsupported cells, and preserve original JSON alongside readable fields. Framework ZIP/XML follows P27's safety pattern; no formula execution or automatic backup restore is added.
- Native controls distinguish matching export from administrator backup; progress/cancellation, scope checks and stale-response guards cover histories, revisions and downloads. Python UI/protocols remain compatible. Ledger import/re-upload proof, representative corpus/scale, shared catalog review and live activation remain separate gates. See CHANGELOG_ARCHIVE.md, Data Bank P31.

### P32 Data Integrity boundary

- `DataIntegrity:Enabled=false` requires PostgreSQL and the authenticated shell. Aggregate inventory/identity/duplicate reads cover the selected home client, including the three existing module stores before their later ports. Home administrators and active assigned managers/internees are explicitly permitted; sharing remains gated. Repeat context/access checks before release; whole-database allocation additionally requires administration.
- Preserve Client Data active template/source identities, Trial Balance upload/account identities and normalized Reconciliation group/account/month comparisons. Retain all 14 baseline index requirements: exact primary/unique scope, leading lookup keys, predicate literals, default key semantics and valid/ready/live/immediate flags. Equivalent renamed/covering lookups pass; no index repair or import-gate relaxation follows.
- Related evidence shares one enforced read-only repeatable snapshot. Savepoints isolate expected schema/permission gaps as Not checked; a visible missing/different index needs attention. Cancellation, timeout and connection failures cannot release partial success. Unknown counts remain unavailable, never zero. Empty clients still require index checks.
- One concurrent scan, at most 60 seconds; defaults bound catalog reads to 1,024 entries/2 MiB and responses to 128 KiB. Bound client-label SQL materialization and refuse oversized results without truncation. Overflow cannot certify a partial catalog. Count/group in SQL and stream upload metadata sizes in constant memory. No row payloads, paths, persistent scans, schema changes or new dependencies.
- Sizes remain estimates: Client Data preserves the legacy character-count/row-overhead basis; upload metadata preserves per-row composite-size projections. Arbitrary legacy source files are unmeasured; invalid/foreign upload links are counted without including foreign metadata. Administrator allocation totals are shared samples, not client usage, snapshot-exact storage or reclaimable bytes.
- Retained tabs/navigation support native disabled, refresh/focus, unavailable-evidence and late-response guards while preserving Python hosting. Passed certifies only the listed baseline, not every upload prerequisite, physical index entries, all values, query speed or storage efficiency. Shared catalog/corpus/scale review and live activation remain separate gates. See CHANGELOG_ARCHIVE.md, Data Integrity P32.

### P33 storage cleanup boundary

- Default-off native cleanup permits selected-home-client administrators to remove eligible temporary plans or one template's current/history/upload dependencies. Unknown ownership, protected evidence, active jobs, transfer grace and unreviewed schema block. Whole-client/global maintenance and compaction remain separately gated.
- Five-minute actor/context/scope-bound preview and completed-backup proofs must match a fresh locked fingerprint; exact typed confirmation precedes one atomic delete. Complete current/history download starts first; initiation cannot certify a saved external copy. Preserve other scopes, Trial Balance, reusable configuration and indexes.
- One apply, 60 seconds, 100,000 evidence rows, 32 MiB captured input, 128 KiB responses and 256 generated artifacts by default. Compact immutable outcomes permit same-request reconciliation without replay; retain at most 1,024 operations per scope before review.
- Post-commit private artifact work uses fenced 150-second leases, 120-second transfers and three attempts. Restart never repeats accounting deletion; pending/review statuses remain explicit. Candidate `0017` is isolated-test only; native flags and startup/live DDL remain off. See CHANGELOG_ARCHIVE.md, Storage cleanup P33.

### P34 Trial Balance parsing boundary

- Unhosted, non-writing parser reuses the accepted XLS/XLSX reader and first-sheet/header contract. Eight distinct required roles plus optional Type bind actual source columns; ambiguous suggestions remain unresolved. Saved mappings, storage, APIs, UI integration and client/action authorization follow later phases.
- Preserve typed source cells, physical Excel positions, effective corrections, nearest-present hierarchy and parent format warnings. Explicit subtotal exclusions and ERP-total/No-total choice govern leaf-only source controls. Invalid amounts, duplicate codes and ERP-total differences over 0.01 block; separate draft Difference lines retain existing advisory roll-forward/global behavior without changing original values.
- Reuse bounded exact arithmetic solely for these controls: up to 1,000 significant digits and adjusted exponents from -1,000 through 1,000. Unsafe source/aggregate/draft legacy conversions require precision review; no import approval or persisted/report numeric policy follows. Binary XLS formula provenance remains unknown; missing XLSX cached results block mapped values.
- Defaults: 100,000 physical data rows, combined 32 MiB serialized input/review evidence, 10,000 issues and 60 seconds with cancellation; accepted reader caps remain ceilings. Refuse overflow, incomplete evidence and changed source hashes. Fingerprints bind effective review choices; `CanImport=false` always. See CHANGELOG_ARCHIVE.md, Trial Balance parsing P34.

### P35 Trial Balance storage boundary

- Unhosted/default-off kernel and isolated-only `0018`; no routes, registration, startup DDL or legacy adoption. Future hosting must establish trusted staged-source ownership. P34 remains non-writing; API/UI/review mutations follow P36.
- Current selected-client home-workspace administrators only. Five-minute signed confirmation binds actor/context, source hash/name/format, interpretation, dates and stored dependencies; reparse/recheck inside the write boundary. Replace exact client/start/end only; preserve overlapping/other periods, other clients, Account Set versions and unknown references.
- One transaction stores canonical rows, separate exact Difference entries, ownership, mandatory audit and immutable compact outcomes. Preserve typed mapped raw cells, blank/zero distinctions, physical source positions, hierarchy, four assignment carry-forward/conflict rules and reusable mappings. Last-upload deletion clears only the category fallback mirror.
- Check actual amount-column types and canonical read-back; reject altered or unsupported values before replacement, without widening columns. Exact comparison grants no new report numeric policy. Native ownership guards reject legacy mutations until their later ports.
- Defaults: one apply/two parsers per process, 60-second deadline, inherited P34 limits, 100,000 snapshot rows/32 MiB, 128 KiB response, 256 uploads/catalog entries and 1,024 retained operations per client; refuse at capacity. Project allocation plus payload/index/audit headroom under the lower configured/C01 ceiling; deletes claim no reclaimed space. Configuration/client/table locks serialize freshness and capacity checks.
- Reused operation IDs return only matching completed outcomes; changed requests/actors conflict. Lost commit acknowledgement permits bounded read-only reconciliation, never automatic write replay. Source paths are trusted adapter inputs, absent from durable outcomes; completed recovery needs no workbook. Representative schema/corpus, restricted-role and production-scale acceptance remain gated. See CHANGELOG_ARCHIVE.md, Trial Balance storage P35.

### P36 saved Trial Balance review

- Review APIs/UI default off and require the authenticated native shell/PostgreSQL. Current selected-client home-workspace administrators only; unowned legacy uploads are read-only. Context assertions and transaction authorization both apply.
- Reuse accepted hierarchy/exact arithmetic. Current saved footings cover every current Leaf independently of paging/search; preserve original ERP controls and saved Difference entries. Unsafe/malformed evidence is Unavailable; empty scope is zero.
- Signed five-minute read/save snapshots bind actor/context, source, target assignments and exact intent. Strict numeric/hyphen code batches preserve occupied-code rules, leading zeros, original codes, manual overrides and four carry-forward/conflict policies. Protected Account Set/unknown dependencies block writes.
- Candidate-only `0019` adds compact immutable review intent/outcomes and narrow code/hierarchy/classification permissions; `0018` stays unchanged. Commit changes, mandatory audit and outcome atomically; verify protected source fields afterward. No amount/raw-cell/adjustment/import-evidence changes or legacy adoption.
- Durable operation IDs reconcile lost acknowledgements/restarts without automatic write replay. Browser drafts survive rejection; unresolved saves retain bounded recovery state. Explicit same-request retry follows a status check and still revalidates/reconciles server-side.
- Defaults: one apply/two readers, 60 seconds, 1 MiB/1,000 explicit edits, 100,000 combined snapshot rows/32 MiB, 1 MiB response, 100-row pages/500 maximum, 2 MiB retained browser state and 1,024 operations/client. Project metadata/mapping/audit growth under C01; refuse overflow.
- Native saved review exposes read-only existing assignments; source upload/replacement/deletion hosting, setup mutations, composite Cashflow/ledger workflows and reports retain later integration gates. No Python fallback, browser source paths, new dependency or live SQL. See CHANGELOG_ARCHIVE.md, Trial Balance review P36.

### P37 profit-confirmation context

- An unhosted pure C# helper evaluates an already-calculated final Income Statement total and its covered categories against mapped Trial Balance profit. It does not calculate full statements or load sources.
- Require a selected final total, active Balance Sheet equity destination, coverage of every nonzero Income Statement category, no nonzero unmapped accounts, and a current matching closing confirmation. Reconcile within an absolute difference of 0.01; `already_in_capital` never applies a second link.
- Use the existing bounded exact-amount type; invalid amounts return unavailable. No float conversion, endpoint, persistence, schema, UI, dependency, Python edit or activation is included. Later report phases may reuse this helper.

### P38 Cash/Bank classification refresh

- After an effective Treasury mapping change, the caller holds one transaction and locks `client_data` before Treasury/settings/source-mapping tables. Reclassify every active row for the configured Cashflows source template with the current native mapping; require matching prior row context and no unrelated profile change. Cap work at 100,000 rows/64 MiB and refuse overflow before writes.
- Update only derived Treasury type/account/status, version and configuration hash on canonical `client_data`. Preserve raw `row_data`, identity, history and voucher-occurrence decisions. Do not rebuild prepared transaction copies or rollups.
- In that transaction, invalidate the client summary cache and advance `cashflow_report_revisions` once. No-op mapping requests skip both. The internal writer remains unhosted until the native configuration-write phase; no route, schema, Python change or activation is included.

### P39 Verification setup

- Keep native Verification setup disabled by default and require the authenticated selected-client home administrator. Assignment writes may change only `verification_workstream`; preserve Financial Statement and Notes categories. Only current Trial Balance leaves are assignable; bulk changes follow the shared hierarchy and dash-delimited subtree boundaries.
- Use the existing Account Set tables. Preserve include/exclude exact and subtree selectors, exclusion precedence, manual leaf overrides, editable drafts, preview, non-empty publication, immutable published versions and one copied revision draft at a time. Audit every mutation in the same transaction.
- No schema, Verification execution, frontend, Python behavior or activation changes are part of P39. Existing Cashflows readers retain the vendor assignment values they use today.

### P40 Reconciliation setup

- Keep `/api/reconciliation` setup routes disabled by default. Require the authenticated selected-client home administrator and repeat ownership checks inside each write transaction; audit every successful mutation atomically.
- Replace groups and balances only within the selected client/group. Accept accounts from the latest parsed Trial Balance, normalize months, reject duplicate account/month pairs, preserve zero as evidence, and keep absent months missing.
- Keep report category sets, definitions and assignments independent of Verification and Financial Statement classifications. Validate parent/active keys and soft-retire/upsert definitions to retain IDs and history.
- No schema, Python, UI, report calculation or activation changes. Release build and isolated PostgreSQL tests cover replacement scope, rollback, tenant isolation, missing/zero balances and category validation.

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

P53A–P56 adds bounded report snapshots, C# statements/Notes/Cashflows, selected-account allocation review, independent balance/posting evidence, default-off working-paper/support/export APIs, OOXML workbooks and owner-bound durable jobs (migration `0020`). Existing REAL/DOUBLE values remain approximate. Exact workbook cell/style parity, full resource-bound acceptance, job/session-revocation and worker-recovery acceptance remain open. Keep options disabled until these checks and end-of-plan database integration acceptance pass.

Proposed job states are queued, preparing_inputs, running, succeeded, failed, cancelled and expired; adapt legacy public statuses separately. Persist owner/lease/attempt/progress, bound retries over immutable inputs and handle worker loss. Calculation/export code does not mutate accounting records. Final job/storage limits require P28/P56/P62 approval; heavy export activation waits for P56 recovery/access checks.

## 7. Dependencies and rollback

| Phase band | Entry condition and compatibility bridge |
|---|---|
| P03–P06 | Retain accepted foundations. Shared-schema execution still needs catalog/capacity evidence, ownership and rollback proof. P06 proves explicit legacy/.NET routing with stubs; no Python report-service client. |
| P07A–P07C, before P08 | Agree foundational direction, then separately approve and rehearse tenant and sharing foundations; resolve detailed policies in relevant phases before enabling operations. Preserve P07 acceptance and existing data; no public switch before proven cross-application authorization. |
| P08–P15 | Tenant membership, engagement assignment, sharing and session/client/role behavior must be consistent across both applications before route switches. Capture response fixtures and verify configuration guards. |
| P16–P29 | Valid client/template/mapping inputs and shared transactions are prerequisites. Settle capacity implementation, hash vectors, coverage/Footing policy, and source revision invalidation before enabling imports. |
| P30–P40 | Preserve Verification/evidence references during purge and both consumers during setup changes. P37 adds profit context; P38 preserves classification/preparation/revision effects; P39 retains Cashflows vendor assignments; P40 keeps report categories independent and balance evidence explicit. |
| P53A–P56, after P40 | Execute eight report steps in the plan: inputs/precision, statements, Notes, Cashflows, reconciliation/evidence, APIs/screens, workbooks and jobs/acceptance. Reuse P37's proven helper; compare source controls, legacy results and exported evidence before switching each operation. |
| P41–P45, after reports | .NET Audit Rules API/compiler implementation complete: tenant-scoped CRUD, client-scoped Signature Specimens, matrix and assertion workflows, template/Form validation, canonical Rulebook and Category snapshots, fingerprints, revision/history guards, and default-off coexistence routes. Python remains authoritative until local database acceptance; do not map data or cut over here. Verification runtime/session rebase and evidence behavior remain P46–P52. |
| P46–P52 | Working sequence: session discovery/start and frozen Rulebook recovery (P46), navigation/read models (P47), atomic line/procedure saves, voucher checks, validation/completion and snapshot-driven Cleared/Query gates (P48), evidence/observations (P49), Forms/Physical Bills/cross-check modal data (P50), Audit Workbook/exports (P51), route parity (P52). P47 preserves Python grouping/order and response fields. Preserve assignment routing, frozen history and authorization. Keep ownership disabled through P52; database integration and acceptance remain P57–P63. |
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
