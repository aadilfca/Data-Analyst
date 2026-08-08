// ==============================================
// 🔎 FEATURE SEARCH INDEX – In-module Feature Registry
// ==============================================
//
// Hand-maintained registry of every *feature* that lives inside a module page, so the global
// navbar search field can find (and jump straight to) things like "Assign Categories" — not just
// the module homepages already described by NAV_MANIFEST (static/client_context.js).
//
// Rules for maintaining this file:
//  - MANUAL ONLY. Nothing here is scraped from the DOM. Adding/renaming/removing a feature in a
//    module means editing this file by hand in the same session, exactly like NAV_MANIFEST.
//  - Only features with a VISIBLE UI ENTRY POINT belong here (a rail tab, a button, a named
//    section a user can actually land on). Backend-only logic, endpoints and helpers do not.
//  - Pages themselves stay in NAV_MANIFEST — this file is strictly the level below that. A
//    feature whose entry point IS a whole page belongs in NAV_MANIFEST, not here.
//
// Entry shape:
//   label    – what the user sees as the result row (the feature's own real UI wording).
//   module   – owning module, used as the breadcrumb prefix ("Audit Rules → Assign Categories").
//              Should match the module's NAV_MANIFEST label so the two read consistently.
//   url      – deep link, always page + exactly one reserved `?feature=<slug>` param (below).
//   keywords – extra lowercase phrases matched by substring alongside `label`. Put synonyms and
//              singular/plural variants a user is likely to type here (e.g. "assign category").
//   state    – 'ready' | 'coming-soon', same values/meaning as NAV_MANIFEST, so the search
//              dropdown's existing ready-vs-disabled row rendering applies unchanged.
//
// Deep-link convention: ONE reserved query param, `?feature=<kebab-case-slug>`, unique across this
// whole array. A query param (not a #hash) to match every other deep link in this app, which are
// all read from the URL at page load — see checkAssignCategoriesDeepLink() in static/audit_rules.js
// and the `?type=` read at the top of static/verification.js. Slugs are additive and safe: a page
// that doesn't yet handle a given slug simply ignores it and loads its normal default view.
//
// Data only — nothing reads this array yet. Search matching is wired separately in
// static/client_context.js, and each slug's on-page handling lives on the target page itself.
const FEATURE_SEARCH_INDEX = [
    // ---------- AUDIT RULES ----------
    // The four rail entities (static/audit_rules.html) — each slug is deliberately the exact
    // argument switchEntityType() already takes in static/audit_rules.js, so the deep-link
    // handler can pass it straight through rather than maintaining a second lookup table.
    {
        label: 'Documents',
        module: 'Audit Rules',
        url: '/static/audit_rules.html?feature=documents',
        keywords: ['supporting document', 'audit document', 'risk rating', 'authorisation roles'],
        state: 'ready',
    },
    {
        label: 'Cross-checks',
        module: 'Audit Rules',
        url: '/static/audit_rules.html?feature=cross-checks',
        keywords: ['cross check', 'crosscheck', 'compare field', 'comparison', 'document chain'],
        state: 'ready',
    },
    {
        label: 'Categories',
        module: 'Audit Rules',
        url: '/static/audit_rules.html?feature=categories',
        keywords: ['category', 'transaction type', 'validation rules', 'cleared requirements'],
        state: 'ready',
    },
    {
        label: 'Voucher-level Checks',
        module: 'Audit Rules',
        url: '/static/audit_rules.html?feature=voucher-checks',
        keywords: ['voucher check', 'prepared by', 'checked by', 'approved by'],
        state: 'ready',
    },
    // Full-page view reached from the Categories list header — swaps out the whole three-pane
    // shell, so it is a genuine in-module destination rather than another rail tab.
    {
        label: 'Assign Categories',
        module: 'Audit Rules',
        url: '/static/audit_rules.html?feature=assign-categories',
        keywords: ['assign category', 'account category', 'trial balance accounts', 'account head'],
        state: 'ready',
    },
    // Section embedded in the Documents detail form (not its own rail tab) — indexed because it
    // has a real, visible entry point a user would search for by name.
    {
        label: 'Signature Specimens',
        module: 'Audit Rules',
        url: '/static/audit_rules.html?feature=signature-specimens',
        keywords: ['signature specimen', 'specimen', 'signatory', 'authority'],
        state: 'ready',
    },
];
