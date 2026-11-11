"""Owned P13 browser fixture and pure Python compatibility oracle; no live DB."""
import sys
from pathlib import Path
from urllib.parse import urlparse
from playwright.sync_api import sync_playwright, expect

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from database import resolve_column_type, resolve_column_comparison

base = sys.argv[1]
assert urlparse(base).hostname in ("127.0.0.1", "localhost")
with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True)
    context = browser.new_context()
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.on("dialog", lambda dialog: dialog.accept())
    page.goto(base + "/static/login.html")
    page.locator("#usernameInput").fill("template-user")
    page.locator("#passwordInput").fill("p13-synthetic")
    page.locator("#loginSubmitBtn").click()
    page.get_by_role("button", name="Firm A — CA firm", exact=True).click()
    expect(page).to_have_url(base + "/static/client_select.html")
    page.locator(".module-card").filter(has_text="Alpha").click()
    expect(page).to_have_url(base + "/static/index.html")
    page.goto(base + "/static/templates.html")
    page.locator("#addTemplateBtn").click()
    page.locator("#templateName").fill("Browser template")
    for name in ("Invoice Reference", "Posting Date"):
        page.locator("#columnSearchInput").fill(name)
        page.locator("#searchResults > div").filter(has_text=name).get_by_role("button", name="Select", exact=True).click()
    page.get_by_label("Reconciliation role for Invoice Reference", exact=True).select_option("document_id")
    page.get_by_label("Reconciliation role for Posting Date", exact=True).select_option("document_date")
    page.locator("#columnSearchInput").fill("Browser total")
    page.locator("#addColumnTriggerBtn").click()
    page.locator("#quickAddColType").select_option("Numeric")
    page.locator("#quickAddSaveBtn").click()
    expect(page.locator("#quickAddColumnModal")).to_be_hidden()
    page.get_by_label("Reconciliation role for Browser total", exact=True).select_option("footing")
    page.get_by_label("Compare for changes: Browser total", exact=True).uncheck()
    page.locator("#saveTemplateBtn").click()
    expect(page.locator("#templateModal")).to_be_hidden()
    row = page.locator("#templatesBody tr").filter(has=page.get_by_role("cell", name="Browser template", exact=True))
    expect(row).to_be_visible()
    get_template = """async name => (await (await fetch('/api/templates')).json()).find(t => t.template_name === name)"""
    original = page.evaluate(get_template, "Browser template")
    assert len(original["columns"]) == 3
    assert all(c["storage_key"].startswith("col_") and c["definition_version"] > 0 for c in original["columns"])
    assert original["columns"][2]["compare_for_changes"] is False
    for column in original["columns"]:
        resolve_column_type(column["data_type"], column["reconciliation_role"])
        assert resolve_column_comparison(column, original) == column["compare_for_changes"]

    bindings = [(c["id"], c["storage_key"]) for c in original["columns"]]
    page.goto(base + "/static/columns.html")
    column_row = page.locator("#columnsBody tr").filter(has=page.get_by_role("cell", name="Browser total", exact=True))
    column_row.get_by_title("Edit column", exact=True).click()
    page.locator("#colName").fill("Browser reviewed total")
    page.locator("#saveColumnBtn").click()
    expect(page.locator("#columnModal")).to_be_hidden()
    page.goto(base + "/static/templates.html")
    expect(row).to_contain_text("Browser reviewed total")
    original = page.evaluate(get_template, "Browser template")
    assert [(c["id"], c["storage_key"]) for c in original["columns"]] == bindings

    # Delayed old responses must not replace a newer template list/cache.
    pending = []
    def out_of_order(route):
        pending.append(route)
        if len(pending) == 1:
            return
        response = route.fetch()
        route.fulfill(response=response)
        pending[0].fulfill(status=200, content_type="application/json", body="[]")
    page.route("**/api/templates", out_of_order)
    page.evaluate("() => { Promise.all([loadTemplates(true), loadTemplates(true)]).then(() => window.p13LoadsDone = true); }")
    page.wait_for_function("window.p13LoadsDone === true")
    expect(row).to_be_visible()
    page.unroute("**/api/templates", out_of_order)

    row.get_by_title("Edit template", exact=True).click()
    # A second administrator's valid save makes the first browser form stale.
    status = page.evaluate("""async t => (await fetch('/api/templates/' + t.id, {
        method:'PUT',headers:{'Content-Type':'application/json','X-Template-Revision':t.configuration_key},
        body:JSON.stringify({template_name:t.template_name,selected_columns:t.columns.map(c=>c.id),has_subtotals_by_default:true})
    })).status""", original)
    assert status == 200
    page.locator("#templateName").fill("Stale overwrite")
    page.locator("#saveTemplateBtn").click()
    expect(page.locator("#modalStatus")).to_contain_text("Reload the template")
    page.locator("#cancelTemplateBtn").click()
    page.reload()
    row.get_by_title("Edit template", exact=True).click()
    expect(page.locator("#hasSubtotalsByDefaultCheckbox")).to_be_checked()
    page.locator("#templateName").fill("Browser <b>Review</b>")
    page.locator("#saveTemplateBtn").click()
    expect(page.locator("#templateModal")).to_be_hidden()
    renamed = page.evaluate(get_template, "Browser <b>Review</b>")
    assert renamed["id"] == original["id"]
    assert [(c["id"], c["storage_key"]) for c in renamed["columns"]] == [(c["id"], c["storage_key"]) for c in original["columns"]]
    row = page.locator("#templatesBody tr").filter(has=page.get_by_role("cell", name="Browser <b>Review</b>", exact=True))
    row.get_by_title("Preview template", exact=True).click()
    expect(page.locator("#previewModal h2")).to_contain_text("Browser <b>Review</b>")
    expect(page.locator("#previewModal h2 b")).to_have_count(0)
    expect(page.locator("#previewModal")).to_contain_text("Browser reviewed total")
    page.locator("#closePreviewBtn").click()
    row.get_by_title("Delete template", exact=True).click()
    expect(row).to_have_count(0)
    page.reload()
    expect(row).to_have_count(0)

    # Prepared ledger reuses the six compatible tenant columns and preserves
    # the configured balance comparison default independently of its label.
    page.locator("#prepareLedgerBtn").click()
    expect(page.locator("#templateModal")).to_be_visible()
    expect(page.get_by_label("Compare for changes: Balance", exact=True)).not_to_be_checked()
    page.locator("#saveTemplateBtn").click()
    expect(page.locator("#templateModal")).to_be_hidden()
    ledger = page.evaluate(get_template, "Account Ledger")
    assert ledger["data_purpose"] == "account_ledger" and len(ledger["ledger_columns"]) == 6
    for column in ledger["columns"]:
        assert resolve_column_comparison(column, ledger) == (column["id"] != ledger["ledger_columns"]["balance"])
    page.get_by_role("link", name="Change workspace", exact=True).click()
    page.get_by_role("button", name="Firm C — CA firm", exact=True).click()
    expect(page).to_have_url(base + "/static/client_select.html")
    page.locator(".module-card").filter(has_text="Gamma").click()
    expect(page).to_have_url(base + "/static/index.html")
    page.goto(base + "/static/templates.html")
    expect(page.locator("#templatesReadOnlyNotice")).to_be_visible()
    expect(page.locator("#addTemplateBtn")).to_be_disabled()
    expect(page.locator("#prepareLedgerBtn")).to_be_disabled()
    expect(page.locator("#templatesBody tr")).to_have_count(1)
    expect(page.locator("#templatesBody tr")).to_contain_text("Firm C template")
    expect(page.get_by_title("Edit template", exact=True)).to_be_disabled()
    denied = page.evaluate("""async () => (await fetch('/api/templates', {method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({template_name:'Denied',selected_columns:[]})})).status""")
    assert denied == 403
    assert not errors, errors

    # Exercise the retained Python protocol with synthetic route responses.
    # These requests never reach a real Python app/database or mutate native data.
    legacy = context.new_page()
    legacy.route("**/shell/context.js", lambda route: route.fulfill(
        response=route.fetch(), body=route.fetch().text() + "\nwindow.AppTenantTemplates = undefined;"))
    legacy_columns = [{"id": 77, "column_name": "Legacy Field", "data_type": "Text", "category": "General", "notes": None}]
    legacy_templates = []
    saved = []
    def legacy_api(route):
        request = route.request
        path = urlparse(request.url).path
        if path == "/api/columns" and request.method == "GET":
            route.fulfill(json=legacy_columns)
        elif path == "/api/templates" and request.method == "GET":
            route.fulfill(json=legacy_templates)
        elif path == "/api/templates" and request.method == "POST":
            payload = request.post_data_json
            assert "column_versions" not in payload and "x-template-revision" not in request.headers
            assert payload["selected_columns"] == [77] and payload["column_roles"] == {"77": "document_id"}
            assert payload["column_comparisons"] == {"77": True}
            saved.append(payload)
            legacy_templates.append({"id": 71, "template_name": payload["template_name"], "columns": legacy_columns,
                                     "is_active": True, "created_date": "2026-03-31", "created_by": "Admin"})
            route.fulfill(json={"template_id": 71, "message": "Template created successfully"})
        else:
            route.fallback()
    legacy.route("**/api/columns", legacy_api)
    legacy.route("**/api/templates", legacy_api)
    legacy.goto(base + "/static/templates.html")
    expect(legacy.locator("#addTemplateBtn")).to_be_enabled()
    legacy.locator("#addTemplateBtn").click()
    legacy.locator("#templateName").fill("Legacy protocol")
    legacy.locator("#columnSearchInput").fill("Legacy Field")
    legacy.locator("#searchResults").get_by_role("button", name="Select", exact=True).click()
    legacy.get_by_label("Reconciliation role for Legacy Field", exact=True).select_option("document_id")
    legacy.locator("#saveTemplateBtn").click()
    expect(legacy.locator("#templateModal")).to_be_hidden()
    expect(legacy.locator("#templatesBody")).to_contain_text("Legacy protocol")
    assert len(saved) == 1
    browser.close()
print("P13 editor, quick-add bindings, ledger defaults, stale saves, safe preview and tenant read-only checks passed")
