"""P12 synthetic browser flow; existing venv/Chromium, no live app or data."""
import sys
from pathlib import Path
from urllib.parse import urlparse
from playwright.sync_api import sync_playwright, expect

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from routers.templates import _master_column_type

base = sys.argv[1]
assert urlparse(base).hostname in ("127.0.0.1", "localhost")
with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True)
    context = browser.new_context()
    page = context.new_page()
    page.on("dialog", lambda dialog: dialog.accept())
    page.goto(base + "/static/login.html")
    page.locator("#usernameInput").fill("column-user")
    page.locator("#passwordInput").fill("p12-synthetic")
    page.locator("#loginSubmitBtn").click()
    page.get_by_role("button", name="Firm A — CA firm", exact=True).click()
    expect(page).to_have_url(base + "/static/client_select.html")
    page.locator(".module-card").filter(has_text="Alpha").click()
    expect(page).to_have_url(base + "/static/index.html")
    page.goto(base + "/static/columns.html")
    expect(page.locator("#columnIdentityNotice")).to_be_visible()
    page.locator("#addColumnBtn").click()
    page.locator("#colName").fill("Browser Amount")
    page.locator("#colType").select_option("Numeric")
    page.locator("#colCategory").fill("Financial")
    page.locator("#saveAndNewBtn").click()
    expect(page.locator("#colName")).to_have_value("")
    expect(page.locator("#columnModal")).to_be_visible()
    page.locator("#colName").fill("Browser Date")
    page.locator("#colType").select_option("Date")
    page.locator("#saveColumnBtn").click()
    expect(page.locator("#columnModal")).to_be_hidden()
    get_column = """async name => (await (await fetch('/api/columns/all')).json()).find(c => c.column_name === name)"""
    original = page.evaluate(get_column, "Browser Date")
    row = page.locator("#columnsBody tr").filter(has=page.get_by_role("cell", name="Browser Date", exact=True))
    row.get_by_title("Edit column", exact=True).click()
    expect(page.locator("#columnUsagePreview")).to_contain_text("0 template(s)")
    page.locator("#colName").fill("Browser Invoice Date")
    page.locator("#saveColumnBtn").click()
    expect(page.locator("#columnModal")).to_be_hidden()
    renamed = page.evaluate(get_column, "Browser Invoice Date")
    assert renamed["id"] == original["id"] and renamed["storage_key"] == original["storage_key"]
    assert renamed["definition_version"] > original["definition_version"]
    for base_type in ("Text", "Numeric", "Date"):
        assert _master_column_type(base_type, None) == base_type
    try:
        _master_column_type("Text", "document_id")
        raise AssertionError("master role should be rejected")
    except Exception as error:
        expected = error.detail
    role_error = page.evaluate("""async () => {
        const r = await fetch('/api/columns', {method:'POST',headers:{'Content-Type':'application/json'},
            body:JSON.stringify({column_name:'Role test',data_type:'Text',reconciliation_role:'document_id'})});
        return {status:r.status,body:await r.json()};
    }""")
    assert role_error["status"] == 400 and role_error["body"]["detail"] == expected
    page.locator("#columnSearchInput").fill("Browser Invoice Date")
    expect(page.locator("#columnsBody tr")).to_have_count(1)
    page.locator("#columnsBody tr").get_by_title("Delete column (soft)", exact=True).click()
    expect(page.locator("#columnsBody tr").get_by_role("cell", name="❌", exact=True)).to_be_visible()
    page.reload()
    expect(page.locator("#columnsBody tr").filter(has=page.get_by_role("cell", name="Browser Invoice Date", exact=True))).to_be_visible()
    page.get_by_role("link", name="Change workspace", exact=True).click()
    page.get_by_role("button", name="Firm C — CA firm", exact=True).click()
    expect(page).to_have_url(base + "/static/client_select.html")
    page.locator(".module-card").filter(has_text="Gamma").click()
    expect(page).to_have_url(base + "/static/index.html")
    page.goto(base + "/static/columns.html")
    expect(page.locator("#columnsReadOnlyNotice")).to_be_visible()
    expect(page.locator("#addColumnBtn")).to_be_disabled()
    expect(page.locator("#columnsBody tr")).to_have_count(1)
    expect(page.locator("#columnsBody tr")).to_contain_text("Read-only field")
    expect(page.locator("#columnsBody tr").get_by_title("Edit column", exact=True)).to_be_disabled()
    denied = page.evaluate("""async () => (await fetch('/api/columns', {method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({column_name:'Denied',data_type:'Text'})})).status""")
    assert denied == 403
    browser.close()
print("P12 browser workflow and Python type/role comparison passed")
