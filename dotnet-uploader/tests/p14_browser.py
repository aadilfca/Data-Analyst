"""Retained-script compatibility fixture against owned native mapping APIs.

P29 serves its own coordinator (including its disabled state). This harness
selects the retained Python-page script only inside intercepted test HTML;
native Upload execution is covered by p29_upload_browser.py instead.
"""
import sys
from urllib.parse import urlparse
from playwright.sync_api import sync_playwright, expect

base, template_id, assigned_template = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
assert urlparse(base).hostname in ("127.0.0.1", "localhost")
headers = ['ERP "Reference" <b>literal</b>', 'ERP Date', 'ERP Amount']
with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True)
    context = browser.new_context()
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.on("dialog", lambda dialog: dialog.accept())
    page.goto(base + "/static/login.html")
    page.locator("#usernameInput").fill("mapping-user")
    page.locator("#passwordInput").fill("p14-synthetic")
    page.locator("#loginSubmitBtn").click()
    page.get_by_role("button", name="Firm A — CA firm", exact=True).click()
    expect(page).to_have_url(base + "/static/client_select.html")
    page.locator(".module-card").filter(has_text="Alpha").click()
    expect(page).to_have_url(base + "/static/index.html")

    def retained_upload_page(route):
        response = route.fetch()
        html = response.text()
        native = '<script src="/shell/upload_native.js"></script>'
        assert html.count(native) == 1
        html = html.replace(native, '<script src="/static/upload.js?v=35"></script>')
        route.fulfill(response=response, body=html)
    context.route("**/static/upload.html", retained_upload_page)

    def open_upload(target=page, template=template_id):
        target.goto(base + "/static/upload.html")
        expect(target.locator(f'#templateDropdown option[value="{template}"]')).to_have_count(1)
        target.locator("#templateDropdown").select_option(str(template))
        target.wait_for_function("nativeTemplateConfigurationKey !== null" if target == page else "templateColumns.length > 0")

    def seed(target=page, matched=False):
        # Only supply the *future staging/auto-map output* to the existing
        # editor. All saved-mapping requests still use the real native API.
        target.evaluate("""({headers, matched}) => {
            ++uploadMappingGeneration;
            fileId='owned-synthetic-staging'; currentSheet='Synthetic sheet';
            excelHeaders=headers;
            currentMappings=templateColumns.map((c,i)=>({...c,column_id:c.id,
                excel_column:matched?headers[i]:null,is_matched:matched}));
            leftPanel.style.display='block'; mappingPanel.style.display='block';
            renderMapping(currentMappings);
        }""", {"headers": headers, "matched": matched})

    get_saved = """async id => (await (await fetch('/api/upload/saved-mapping?client_id=10&template_id='+id)).json())"""
    open_upload()
    seed(matched=True)
    page.locator("#saveMappingBtn").click()
    expect(page.locator("#statusMessage")).to_contain_text("Column mapping saved")
    saved = page.evaluate(get_saved, template_id)
    assert saved["has_saved_mapping"] and len(saved["mappings"]) == 3
    assert all(m["storage_key"].startswith("col_") for m in saved["mappings"])
    assert saved["mapping_revision"] > 0
    seed()
    assert page.evaluate("applySavedMapping()") == 3
    page.evaluate("renderMapping(currentMappings)")
    assert page.locator('#mappingBody select[data-column-id]').evaluate_all("items=>items.map(e=>e.value)") == headers
    expect(page.locator("#mappingBody b")).to_have_count(0)
    before_clear = page.evaluate("currentMappings.map(m=>m.excel_column)")
    page.locator("#clearMappingBtn").click()
    expect(page.locator("#statusMessage")).to_contain_text("Saved mapping cleared")
    assert page.evaluate("currentMappings.map(m=>m.excel_column)") == before_clear
    cleared = page.evaluate(get_saved, template_id)
    assert not cleared["has_saved_mapping"] and cleared["mapping_revision"] > saved["mapping_revision"]
    seed()
    assert page.evaluate("applySavedMapping()") == 0

    # Another valid writer advances the revision; an older editor cannot save
    # over it. Reads are explicit; writes are never automatically retried.
    seed(matched=True)
    assert page.evaluate("applySavedMapping()") == 0
    status = page.evaluate("""async ({id,snapshot,header}) => (await fetch('/api/upload/save-mapping',{
        method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({client_id:10,template_id:id,
            mapping_revision:snapshot.mapping_revision,configuration_key:snapshot.configuration_key,
            mappings:[{template_column_id:templateColumns[0].id,excel_column:header}]})})).status""",
        {"id": template_id, "snapshot": cleared, "header": headers[0]})
    assert status == 200
    page.locator("#saveMappingBtn").click()
    expect(page.locator("body")).to_contain_text("Saved mapping or template changed")
    # Close any shared failure dialog through Escape before continuing.
    page.keyboard.press("Escape")

    # Matching is exact: a saved header absent from this sheet is not applied.
    seed()
    page.evaluate("excelHeaders=['different ERP header'];")
    assert page.evaluate("applySavedMapping()") == 0
    actual = page.evaluate(get_saved, template_id)
    for changed in ("file", "sheet", "template", "generation", "client"):
        seed()
        pending = []
        def hold(route):
            pending.append(route)
        page.route("**/api/upload/saved-mapping?*", hold)
        with page.expect_request(lambda request: "/api/upload/saved-mapping?" in request.url):
            page.evaluate("() => { window.p14ApplyDone=null; applySavedMapping().then(n=>window.p14ApplyDone=n); }")
        page.evaluate("""kind => {
            if(kind==='file') fileId='different-file';
            if(kind==='sheet') currentSheet='different-sheet';
            if(kind==='template') {window.p14OldTemplate=selectedTemplateId;selectedTemplateId=-1;}
            if(kind==='generation') ++uploadMappingGeneration;
            if(kind==='client') {window.p14OldClient=getSelectedClientId;window.getSelectedClientId=()=>11;}
        }""", changed)
        assert len(pending) == 1
        pending[0].fulfill(json=actual)
        page.wait_for_function("window.p14ApplyDone !== null")
        assert page.evaluate("window.p14ApplyDone") == 0
        assert page.evaluate("currentMappings.every(m=>m.excel_column===null)")
        page.unroute("**/api/upload/saved-mapping?*", hold)
        page.evaluate("""() => {
            if(window.p14OldTemplate) {selectedTemplateId=window.p14OldTemplate;delete window.p14OldTemplate;}
            if(window.p14OldClient) {window.getSelectedClientId=window.p14OldClient;delete window.p14OldClient;}
        }""")

    # Assigned firm staff receive P14's specific action grant, independently of
    # their tenant member role. Other-client denial is covered by backend tests.
    page.goto(base + "/static/index.html")
    page.get_by_role("link", name="Change workspace", exact=True).click()
    page.get_by_role("button", name="Firm C — CA firm", exact=True).click()
    expect(page).to_have_url(base + "/static/client_select.html")
    page.locator(".module-card").filter(has_text="Gamma").click()
    expect(page).to_have_url(base + "/static/index.html")
    open_upload(template=assigned_template)
    seed(matched=True)
    page.locator("#saveMappingBtn").click()
    expect(page.locator("#statusMessage")).to_contain_text("Column mapping saved")
    assert not errors, errors

    # Legacy payload compatibility, with only the three saved-mapping calls
    # mocked. No legacy database or native save receives these protocol probes.
    legacy = context.new_page()
    def legacy_context(route):
        response = route.fetch()
        route.fulfill(response=response, body=response.text()+"\nwindow.AppTenantMappings=undefined;")
    legacy.route("**/shell/context.js", legacy_context)
    captured = []
    def legacy_mapping(route):
        request = route.request
        if request.method == "GET":
            route.fulfill(json={"has_saved_mapping": False, "mappings": []})
        elif request.method == "POST":
            body = request.post_data_json
            assert set(body) == {"client_id", "template_id", "mappings"}
            captured.append(body)
            route.fulfill(json={"message": "Saved", "saved_count": len(body["mappings"])})
        else:
            assert "x-mapping-revision" not in request.headers and "x-template-revision" not in request.headers
            captured.append("clear")
            route.fulfill(json={"message": "Cleared", "deleted_count": 1})
    legacy.route("**/api/upload/save-mapping", legacy_mapping)
    legacy.route("**/api/upload/saved-mapping?*", legacy_mapping)
    legacy.on("dialog", lambda dialog: dialog.accept())
    open_upload(legacy, assigned_template)
    seed(legacy, True)
    legacy.locator("#saveMappingBtn").click()
    expect(legacy.locator("#statusMessage")).to_contain_text("Column mapping saved")
    legacy.locator("#clearMappingBtn").click()
    expect(legacy.locator("#statusMessage")).to_contain_text("Saved mapping cleared")
    assert len(captured) == 2
    browser.close()
print("P14 mapping persistence/reuse, exact headers, scope races, assigned staff and legacy protocol passed")
