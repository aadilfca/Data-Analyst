"""Hermetic browser coverage for the simplified Account Control Centre grid."""

import functools
import json
import os
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

import pytest

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import sync_playwright  # noqa: E402

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class _QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


@pytest.fixture(scope="module")
def static_server():
    handler = functools.partial(_QuietHandler, directory=PROJECT_ROOT)
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield "http://127.0.0.1:{}".format(server.server_port)
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as playwright:
        try:
            instance = playwright.chromium.launch(headless=True)
        except Exception as exc:
            pytest.skip("Chromium not available: {}".format(exc))
        try:
            yield instance
        finally:
            instance.close()


def choose_financial_category(page, account_code, category_id):
    row = page.locator("#accFinancialRows tr:has(select.acc2-financial-category[data-acc-code='{}'])".format(account_code))
    row.locator("[data-fs-picker-trigger]").click()
    row.locator("[data-fs-picker-option][data-fs-picker-value='{}']".format(category_id)).click()


def choose_note(page, account_code, note_id):
    row = page.locator("#accNotesRows tr:has(select.acc2-note-category[data-acc-code='{}'])".format(account_code))
    row.locator("[data-fs-picker-trigger]").click()
    row.locator("[data-fs-picker-option][data-fs-picker-value='{}']".format(note_id)).click()


def test_verification_grid_loads_saves_and_applies_parent_assignment(browser, static_server):
    mapping = {
        "client_id": 1,
        "upload": {"id": 77, "file_name": "January Trial Balance.xlsx", "as_of_date": "2026-01-31"},
        "verification_workstreams": ["bank", "cash", "local_vendor"],
        "accounts": [
            {"id": 1, "account_code": "20-15-50", "account_name": "Local creditors", "parent_account_code": None, "level": 3, "is_leaf": False, "assignment": {"verification_workstream": None}},
            {"id": 2, "account_code": "20-15-50-0001", "account_name": "Supplier A", "parent_account_code": "20-15-50", "level": 4, "is_leaf": True, "assignment": {"verification_workstream": None, "financial_statement_category_id": None, "note_category_id": None}},
            {"id": 3, "account_code": "20-15-50-0002", "account_name": "Supplier B", "parent_account_code": "20-15-50", "level": 4, "is_leaf": True, "assignment": {"verification_workstream": "bank", "financial_statement_category_id": None, "note_category_id": None}},
            {"id": 4, "account_code": "10-15-115-10-0001", "account_name": "Cash in hand", "parent_account_code": None, "level": 5, "is_leaf": True, "assignment": {"verification_workstream": "cash", "financial_statement_category_id": None, "note_category_id": None}},
        ],
    }
    mapping["categories"] = [
        {"id": 10, "client_id": 1, "category_type": "financial_statement", "category_key": "current_assets", "label": "Current assets", "sort_order": 0, "is_active": True},
        {"id": 11, "client_id": 1, "category_type": "financial_statement", "category_key": "current_liabilities", "label": "Current liabilities", "sort_order": 1, "is_active": True},
        {"id": 12, "client_id": 1, "category_type": "financial_statement", "category_key": "fixed_assets", "label": "Fixed Assets", "statement_section": None, "row_type": "category", "sort_order": 2, "is_active": True},
        {"id": 30, "client_id": 1, "category_type": "financial_statement", "category_key": "non_current_assets", "label": "Non Current Assets", "statement_section": "balance_sheet", "row_type": "heading", "parent_category_id": None, "sort_order": 3, "is_active": True},
        {"id": 31, "client_id": 1, "category_type": "financial_statement", "category_key": "property", "label": "Property", "statement_section": "balance_sheet", "row_type": "category", "parent_category_id": 30, "sort_order": 4, "is_active": True},
        {"id": 32, "client_id": 1, "category_type": "financial_statement", "category_key": "equipment", "label": "Equipment", "statement_section": "balance_sheet", "row_type": "category", "parent_category_id": 30, "sort_order": 5, "is_active": True},
        {"id": 40, "client_id": 1, "category_type": "financial_statement", "category_key": "current_assets_heading", "label": "Current Assets Heading", "statement_section": "balance_sheet", "row_type": "heading", "parent_category_id": None, "sort_order": 6, "is_active": True},
        {"id": 41, "client_id": 1, "category_type": "financial_statement", "category_key": "stock", "label": "Stock", "statement_section": "balance_sheet", "row_type": "category", "parent_category_id": 40, "sort_order": 7, "is_active": True},
        {"id": 42, "client_id": 1, "category_type": "financial_statement", "category_key": "cash_equivalents", "label": "Cash equivalents", "statement_section": "balance_sheet", "row_type": "category", "parent_category_id": 40, "sort_order": 8, "is_active": True},
    ]
    mapping["note_categories"] = [
        {"id": 20, "client_id": 1, "category_type": "note", "category_key": "accounting_policies", "label": "Accounting policies", "sort_order": 0, "is_active": True},
    ]
    mapping["statement_calculations"] = []
    calls = []

    def handle_api(route, request):
        url = request.url
        if "/api/auth/current-client" in url:
            route.fulfill(status=200, content_type="application/json", body=json.dumps({"client_id": 1, "client_name": "Regression Client", "client_code": "RC"}))
            return
        if "/api/users" in url:
            route.fulfill(status=200, content_type="application/json", body=json.dumps([]))
            return
        if "/api/account-control-center/statement-calculations" in url:
            if request.method == "GET":
                route.fulfill(status=200, content_type="application/json", body=json.dumps({"calculations": mapping["statement_calculations"]}))
            elif request.method == "POST":
                body = json.loads(request.post_data or "{}")
                body["id"] = max([item["id"] for item in mapping["statement_calculations"]] or [100]) + 1
                for component in body.get("components", []):
                    if component.get("source_category_id") is not None:
                        source = next(item for item in mapping["categories"] if item["id"] == component["source_category_id"])
                        component["label"] = source["label"]
                    elif component.get("source_calculation_id") is not None:
                        source = next(item for item in mapping["statement_calculations"] if item["id"] == component["source_calculation_id"])
                        component["label"] = source["label"]
                body.setdefault("sort_order", len(mapping["statement_calculations"]))
                mapping["statement_calculations"].append(body)
                calls.append((request.method, url, body))
                route.fulfill(status=200, content_type="application/json", body=json.dumps({"saved": True, "calculation_id": body["id"]}))
            elif request.method == "PUT":
                body = json.loads(request.post_data or "{}")
                calculation_id = int(url.split("/statement-calculations/")[1])
                calculation = next(item for item in mapping["statement_calculations"] if item["id"] == calculation_id)
                calculation.update(body)
                for component in calculation.get("components", []):
                    if component.get("source_category_id") is not None:
                        source = next(item for item in mapping["categories"] if item["id"] == component["source_category_id"])
                        component["label"] = source["label"]
                    elif component.get("source_calculation_id") is not None:
                        source = next(item for item in mapping["statement_calculations"] if item["id"] == component["source_calculation_id"])
                        component["label"] = source["label"]
                calls.append((request.method, url, body))
                route.fulfill(status=200, content_type="application/json", body=json.dumps({"saved": True}))
            elif request.method == "DELETE":
                calculation_id = int(url.split("/statement-calculations/")[1].split("?")[0])
                mapping["statement_calculations"] = [item for item in mapping["statement_calculations"] if item["id"] != calculation_id]
                calls.append((request.method, url, {"calculation_id": calculation_id}))
                route.fulfill(status=200, content_type="application/json", body=json.dumps({"deleted": True}))
            return
        if "/api/account-control-center/categories" in url:
            is_note = "category_type=note" in url
            if request.method == "GET":
                category_list = mapping["note_categories"] if is_note else mapping["categories"]
                category_list.sort(key=lambda item: (item.get("sort_order", 0), item.get("label", "").lower(), item.get("category_key", "")))
                route.fulfill(status=200, content_type="application/json", body=json.dumps({"categories": category_list}))
            elif request.method == "POST":
                body = json.loads(request.post_data or "{}")
                is_note = body.get("category_type") == "note"
                category_list = mapping["note_categories"] if is_note else mapping["categories"]
                body["id"] = max([int(item["id"]) for item in category_list] or [20 if is_note else 11]) + 1
                body["is_active"] = True
                if is_note: body["assigned_account_count"] = 1 if body.get("source_statement_category_id") else 0
                category_list.append(body)
                calls.append((request.method, url, body))
                route.fulfill(status=200, content_type="application/json", body=json.dumps({"saved": True, "category": body}))
            elif request.method == "PUT" and url.split("?")[0].endswith("/categories/order"):
                body = json.loads(request.post_data or "{}")
                calls.append((request.method, url, body))
                category_list = mapping["note_categories"] if body.get("category_type") == "note" else mapping["categories"]
                for order, category_id in enumerate(body["category_ids"]):
                    item = next(item for item in category_list if item["id"] == category_id)
                    item["sort_order"] = order * 10
                    if body.get("category_type") == "note": item["parent_category_id"] = body.get("parent_category_id")
                route.fulfill(status=200, content_type="application/json", body=json.dumps({"saved": True, "category_ids": body["category_ids"]}))
            elif request.method == "PUT":
                body = json.loads(request.post_data or "{}")
                is_note = body.get("category_type") == "note"
                category_list = mapping["note_categories"] if is_note else mapping["categories"]
                category_id = int(url.split("/categories/")[1])
                category = next(item for item in category_list if item["id"] == category_id)
                category.update({key: value for key, value in body.items() if key != "client_id"})
                calls.append((request.method, url, body))
                route.fulfill(status=200, content_type="application/json", body=json.dumps({"saved": True, "category": category}))
            elif request.method == "DELETE":
                category_id = int(url.split("/categories/")[1].split("?")[0])
                removed = [item for item in mapping["categories"] if item["id"] == category_id or item.get("parent_category_id") == category_id]
                mapping["categories"] = [item for item in mapping["categories"] if item not in removed]
                calls.append((request.method, url, {"category_id": category_id, "deleted_child_count": len(removed) - 1}))
                route.fulfill(status=200, content_type="application/json", body=json.dumps({"deleted": True, "category_id": category_id, "deleted_child_count": len(removed) - 1}))
            else:
                route.fulfill(status=200, content_type="application/json", body=json.dumps({"saved": True}))
            return
        if "/api/account-control-center/mapping/apply-to-children" in url:
            body = json.loads(request.post_data or "{}")
            calls.append((request.method, url, body))
            for account in mapping["accounts"]:
                if account["is_leaf"] and account["account_code"].startswith(body["parent_account_code"] + "-"):
                    for field in ("verification_workstream", "financial_statement_category_id", "note_category_id"):
                        if field in body:
                            account["assignment"][field] = body[field]
            route.fulfill(status=200, content_type="application/json", body=json.dumps({"saved": True, "leaf_count": 2}))
            return
        if url.split("?")[0].endswith("/api/account-control-center/mapping"):
            if request.method == "PUT":
                body = json.loads(request.post_data or "{}")
                calls.append((request.method, url, body))
                for account in mapping["accounts"]:
                    if account["account_code"] == body["account_code"]:
                        for field in ("verification_workstream", "financial_statement_category_id", "note_category_id"):
                            if field in body:
                                account["assignment"][field] = body[field]
                route.fulfill(status=200, content_type="application/json", body=json.dumps({"saved": True}))
            else:
                route.fulfill(status=200, content_type="application/json", body=json.dumps(mapping))
            return
        route.fulfill(status=200, content_type="application/json", body="{}")

    page = browser.new_page()
    page.add_init_script("""(() => {
        window.__accOriginalFetch = window.fetch;
        window.__accReleaseMapping = null;
        window.fetch = (input, init) => {
            if (String(input).includes('/api/account-control-center/mapping?')) {
                return new Promise(resolve => {
                    window.__accReleaseMapping = () => window.__accOriginalFetch(input, init).then(resolve);
                });
            }
            return window.__accOriginalFetch(input, init);
        };
    })();""")
    page.route("**/api/**", handle_api)
    page.goto("{}/static/account_control_center.html".format(static_server), wait_until="domcontentloaded")
    page.wait_for_selector("#accPageBusy:not([hidden])")
    page.wait_for_function("window.__accReleaseMapping !== null")
    assert page.locator("#accPageBusy").get_attribute("aria-busy") == "true"
    assert page.locator("#accGridState.loading").is_visible()
    page.evaluate("window.__accReleaseMapping()")
    assert page.locator(".acc2-header").count() == 0
    assert page.locator("#accStatus").is_hidden()
    page.wait_for_selector("#accAccountRows .acc2-workstream")
    page.wait_for_selector("#accPageBusy", state="hidden")
    assert page.locator("#accTabFinancial").is_enabled()
    page.evaluate("window.fetch = window.__accOriginalFetch")
    assert page.locator("#accSourceInfo").text_content() == "Source: January Trial Balance.xlsx - 2026-01-31"
    assert page.locator("#accTreasuryGuidance").is_hidden()
    page.locator("#accTreasuryGuidanceToggle").click()
    assert page.locator("#accTreasuryGuidance").is_visible()
    assert "credit" in page.locator("#accTreasuryGuidance").text_content().lower()
    assert "debit" in page.locator("#accTreasuryGuidance").text_content().lower()
    page.locator("#accTreasuryGuidanceClose").click()
    assert page.locator("#accTreasuryGuidance").is_hidden()
    assert page.locator("#accAccountRows tr").count() == 4
    assert page.locator("select[data-acc-code='10-15-115-10-0001']").input_value() == "cash"
    assert page.locator("select[data-acc-code='20-15-50'] option:checked").text_content() == "Mixed assignments - choose one"

    page.evaluate("""() => {
        window.__accOriginalFetch = window.fetch;
        window.__accReleaseLeaf = null;
        window.fetch = (input, init) => {
            if (String(input).endsWith('/api/account-control-center/mapping')) {
                return new Promise(resolve => {
                    window.__accReleaseLeaf = () => window.__accOriginalFetch(input, init).then(resolve);
                });
            }
            return window.__accOriginalFetch(input, init);
        };
    }""")
    page.locator("select[data-acc-code='10-15-115-10-0001']").select_option("local_vendor")
    page.wait_for_function("window.__accReleaseLeaf !== null")
    page.wait_for_function("!document.querySelector('#accPageBusy').hidden")
    assert page.locator("select[data-acc-code='10-15-115-10-0001']").is_disabled()
    assert page.locator("[data-acc-result='10-15-115-10-0001']").locator(".acc2-inline-saving").is_visible()
    page.evaluate("window.__accReleaseLeaf()")
    page.wait_for_function("document.querySelector('#accPageBusy').hidden")
    assert page.locator("select[data-acc-code='10-15-115-10-0001']").is_enabled()
    assert page.locator("[data-acc-result='10-15-115-10-0001']").text_content() == "Saved"
    page.evaluate("window.fetch = window.__accOriginalFetch")
    assert any(call[0] == "PUT" and call[2]["verification_workstream"] == "local_vendor" for call in calls)

    page.evaluate("""() => {
        window.__accOriginalFetch = window.fetch;
        window.__accReleaseParent = null;
        window.fetch = (input, init) => {
            if (String(input).includes('/api/account-control-center/mapping/apply-to-children')) {
                return new Promise(resolve => {
                    window.__accReleaseParent = () => window.__accOriginalFetch(input, init).then(resolve);
                });
            }
            return window.__accOriginalFetch(input, init);
        };
    }""")
    page.once("dialog", lambda dialog: dialog.accept())
    page.locator("select[data-acc-code='20-15-50']").select_option("local_vendor")
    page.wait_for_function("window.__accReleaseParent !== null")
    page.wait_for_function("!document.querySelector('#accPageBusy').hidden")
    assert page.locator("#accGridWrap").get_attribute("aria-busy") == "true"
    assert page.locator("#accGridWrap.acc2-processing").is_visible()
    assert page.locator("select[data-acc-code='20-15-50']").is_disabled()
    page.evaluate("window.__accReleaseParent()")
    page.wait_for_function("document.querySelector('#accPageBusy').hidden")
    page.wait_for_timeout(100)
    page.evaluate("window.fetch = window.__accOriginalFetch")
    assert any(call[0] == "POST" and call[2]["parent_account_code"] == "20-15-50" for call in calls)
    assert page.locator("select[data-acc-code='20-15-50']").input_value() == "local_vendor"
    main_notice = page.locator("#accStatus.acc2-main-toast.success")
    assert main_notice.is_visible()
    assert main_notice.evaluate("element => getComputedStyle(element).position") == "fixed"
    assert main_notice.evaluate("element => getComputedStyle(element).right") == "20px"

    page.evaluate("""() => {
        window.__accOriginalFetch = window.fetch;
        window.__accReleaseFinancial = null;
        window.fetch = (input, init) => {
            const url = String(input);
            if (url.includes('/api/account-control-center/categories') && url.includes('category_type=financial_statement')) {
                return new Promise(resolve => {
                    window.__accReleaseFinancial = () => window.__accOriginalFetch(input, init).then(resolve);
                });
            }
            return window.__accOriginalFetch(input, init);
        };
    }""")
    page.locator("#accTabFinancial").click()
    page.wait_for_selector("#accPageBusy:not([hidden])")
    page.wait_for_selector("#accFinancialGridState.loading")
    assert page.locator("#accTabFinancial").is_disabled()
    page.wait_for_function("window.__accReleaseFinancial !== null")
    page.evaluate("window.__accReleaseFinancial()")
    page.wait_for_selector("#accFinancialRows .acc2-financial-category", state="attached")
    page.wait_for_selector("#accPageBusy", state="hidden")
    assert page.locator("#accTabFinancial").is_enabled()
    page.evaluate("window.fetch = window.__accOriginalFetch")
    picker_row = page.locator("#accFinancialRows tr:has(select[data-acc-code='20-15-50-0001'])")
    picker_trigger = picker_row.locator("[data-fs-picker-trigger]")
    picker_trigger.click()
    picker = picker_row.locator("[data-fs-picker]")
    picker_sections = picker.locator(".acc2-fs-picker-section-title")
    assert picker_sections.all_text_contents()[:2] == ["Balance Sheet", "Income Statement"]
    assert picker.locator(".acc2-fs-picker-heading").get_by_text("Non Current Assets").is_visible()
    picker_search = picker.locator("[data-fs-picker-search-input]")
    picker_search.fill("Equipment")
    assert picker.locator("[data-fs-picker-option]:visible").count() == 1
    assert "Equipment" in picker.locator("[data-fs-picker-option]:visible").first.text_content()
    picker_search.fill("")
    assert picker_sections.nth(0).is_visible()
    with page.expect_response(lambda response: response.url.endswith("/api/account-control-center/mapping") and response.request.method == "PUT"):
        picker.locator("[data-fs-picker-option][data-fs-picker-value='32']").click()
    assert page.locator("select.acc2-financial-category[data-acc-code='20-15-50-0001']").input_value() == "32"
    assert any(call[0] == "PUT" and call[2].get("financial_statement_category_id") == 32 for call in calls)
    with page.expect_response(lambda response: response.url.endswith("/api/account-control-center/mapping") and response.request.method == "PUT"):
        choose_financial_category(page, "20-15-50-0001", 10)
    page.evaluate("""() => {
        window.__accOriginalFetch = window.fetch;
        window.__accReleaseFinancialLeaf = null;
        window.fetch = (input, init) => String(input).endsWith('/api/account-control-center/mapping') && init && init.method === 'PUT'
            ? new Promise(resolve => { window.__accReleaseFinancialLeaf = () => window.__accOriginalFetch(input, init).then(resolve); })
            : window.__accOriginalFetch(input, init);
    }""")
    choose_financial_category(page, "20-15-50-0001", 11)
    page.wait_for_function("window.__accReleaseFinancialLeaf !== null")
    assert page.locator("#accPageBusy").is_hidden()
    assert page.locator("#accFinancialRows [data-acc-fs-result='20-15-50-0001'] .acc2-inline-saving").is_visible()
    page.evaluate("window.__accReleaseFinancialLeaf()")
    page.wait_for_function("document.querySelector('#accFinancialRows [data-acc-fs-result=\"20-15-50-0001\"]')?.textContent === 'Saved'")
    page.evaluate("window.fetch = window.__accOriginalFetch")
    assert any(call[0] == "PUT" and call[2].get("financial_statement_category_id") == 10 and "verification_workstream" not in call[2] for call in calls)
    assert any(call[0] == "PUT" and call[2].get("financial_statement_category_id") == 11 and "verification_workstream" not in call[2] for call in calls)
    assert page.locator("#accFinancialCategoryForm").count() == 0
    financial_fab = page.locator("#accFinancialSetupFab")
    assert financial_fab.is_visible()
    financial_fab.click()
    assert page.locator("#accFinancialSetupModal").is_visible()
    assert page.locator("#accStatus").is_hidden()
    setup_tabs = page.locator("[data-acc-financial-setup-tab]")
    assert setup_tabs.all_text_contents() == ["Balance Sheet", "Notes to Balance Sheet", "Income Statement", "Notes to Income Statement"]
    assert page.locator("#accFinancialSetupPanelBalanceSheet [data-fs-action='add-heading']").is_visible()
    assert page.locator("#accFinancialSetupPanelBalanceSheet .acc2-fs-unplaced").get_by_text("Fixed Assets").is_visible()
    assert page.locator("#accFinancialSetupPanelBalanceSheet [data-fs-id='30'][data-fs-action='delete']").is_visible()
    assert page.locator("#accFinancialSetupPanelBalanceSheet .acc2-fs-category").filter(has_text="Property").locator("[data-fs-action='delete']").is_visible()
    property_handle = page.locator("#accFinancialSetupPanelBalanceSheet [data-fs-sort-row][data-fs-id='31'] [data-fs-drag-handle]")
    stock_row = page.locator("#accFinancialSetupPanelBalanceSheet [data-fs-sort-row][data-fs-id='41']")
    property_handle.drag_to(stock_row, target_position={"x": 40, "y": stock_row.bounding_box()["height"] - 2})
    assert not any(call[0] == "PUT" and call[1].split("?")[0].endswith("/categories/order") for call in calls)
    equipment_row = page.locator("#accFinancialSetupPanelBalanceSheet [data-fs-sort-row][data-fs-id='32']")
    page.evaluate("""() => {
        window.__accReleaseFinancialOrder = null;
        window.fetch = (input, init) => String(input).endsWith('/api/account-control-center/categories/order')
            ? new Promise(resolve => { window.__accReleaseFinancialOrder = () => window.__accOriginalFetch(input, init).then(resolve); })
            : window.__accOriginalFetch(input, init);
    }""")
    property_handle.drag_to(equipment_row, target_position={"x": 40, "y": equipment_row.bounding_box()["height"] - 2})
    page.wait_for_function("window.__accReleaseFinancialOrder !== null")
    assert page.locator("#accFinancialSetupPanelBalanceSheet").get_attribute("aria-busy") == "true"
    assert page.locator("#accFinancialSetupPanelBalanceSheet [data-fs-order-status] .ui-spinner").is_visible()
    assert "Saving order" in page.locator("#accFinancialSetupPanelBalanceSheet [data-fs-order-status]").text_content()
    page.evaluate("window.__accReleaseFinancialOrder()")
    page.wait_for_function("document.querySelector('#accFinancialSetupPanelBalanceSheet').getAttribute('aria-busy') !== 'true'")
    page.evaluate("window.fetch = window.__accOriginalFetch")
    page.wait_for_function("document.querySelector('[data-fs-sort-group=\\\"children:30\\\"] [data-fs-sort-row]')?.textContent.includes('Equipment')")
    assert any(call[0] == "PUT" and call[2].get("category_ids") == [32, 31] for call in calls)
    heading_handle = page.locator("#accFinancialSetupPanelBalanceSheet > .acc2-fs-heading-list > [data-fs-sort-kind='heading'][data-fs-id='30'] > .acc2-fs-heading-title [data-fs-drag-handle]")
    current_heading = page.locator("#accFinancialSetupPanelBalanceSheet > .acc2-fs-heading-list > [data-fs-sort-kind='heading'][data-fs-id='40']")
    heading_handle.drag_to(current_heading, target_position={"x": 45, "y": current_heading.bounding_box()["height"] - 2})
    page.wait_for_function("document.querySelector('[data-fs-sort-group=\\\"headings:balance_sheet\\\"] [data-fs-sort-row]')?.dataset.fsId === '40'")
    assert any(call[0] == "PUT" and call[2].get("category_ids") == [40, 30] for call in calls)
    page.once("dialog", lambda dialog: dialog.accept())
    page.locator("#accFinancialSetupPanelBalanceSheet .acc2-fs-unplaced .acc2-fs-category").filter(has_text="Fixed Assets").locator("[data-fs-action='delete']").click()
    page.wait_for_function("!document.querySelector('#accFinancialSetupPanelBalanceSheet .acc2-fs-unplaced') || !document.querySelector('#accFinancialSetupPanelBalanceSheet .acc2-fs-unplaced').textContent.includes('Fixed Assets')")
    assert any(call[0] == "DELETE" and call[2]["category_id"] == 12 for call in calls)
    cascade_dialogs = []
    page.once("dialog", lambda dialog: (cascade_dialogs.append(dialog.message), dialog.accept()))
    page.locator("#accFinancialSetupPanelBalanceSheet [data-fs-id='30'][data-fs-action='delete']").click()
    page.wait_for_function("!document.querySelector('#accFinancialSetupPanelBalanceSheet').textContent.includes('Non Current Assets')")
    assert "all 2 child categories" in cascade_dialogs[0]
    assert any(call[0] == "DELETE" and call[2]["category_id"] == 30 and call[2]["deleted_child_count"] == 2 for call in calls)
    popup_notice = page.locator("#accFinancialSetupModal [data-acc-popup-notice]")
    assert popup_notice.is_visible()
    assert popup_notice.get_attribute("role") == "status"
    assert popup_notice.evaluate("""element => {
        const a = element.getBoundingClientRect();
        const b = element.closest('.acc2-modal').querySelector('.acc2-modal-tabs').getBoundingClientRect();
        return a.bottom <= b.top || a.top >= b.bottom;
    }""")
    popup_notice.wait_for(state="hidden", timeout=6000)
    setup_tabs.nth(1).click()
    assert setup_tabs.nth(1).get_attribute("aria-selected") == "true"
    assert page.locator("#accFinancialSetupPanelNotesBalanceSheet").get_attribute("hidden") is None
    notes_bs = page.locator("#accFinancialSetupPanelNotesBalanceSheet")
    assert notes_bs.locator(".acc2-notes-group").get_by_text("Current Assets").is_visible()
    assert notes_bs.locator(".acc2-notes-source-label").get_by_text("Stock").is_visible()
    notes_bs.locator("[data-source-id='41'][data-note-action='link']").click()
    page.wait_for_function("document.querySelector('#accFinancialSetupPanelNotesBalanceSheet [data-note-source-drop][data-source-id=\"41\"][data-note-anchor-id=\"21\"]') !== null")
    assert notes_bs.locator("[data-note-source-drop][data-source-id='41'][data-note-anchor-id='21']").count() == 1
    move_confirmations = []
    page.once("dialog", lambda dialog: (move_confirmations.append(dialog.message), dialog.accept()))
    notes_bs.locator("[data-note-anchor-id='21'][data-note-anchor-drag-handle]").drag_to(notes_bs.locator("[data-note-source-drop][data-source-id='42']"))
    page.wait_for_function("document.querySelector('#accFinancialSetupPanelNotesBalanceSheet [data-note-source-drop][data-source-id=\"42\"] [data-note-anchor-id=\"21\"]') !== null")
    assert "1 currently assigned Trial Balance account" in move_confirmations[0]
    assert any(item["id"] == 21 and item["source_statement_category_id"] == 42 for item in mapping["note_categories"])
    page.once("dialog", lambda dialog: dialog.accept("Raw material"))
    notes_bs.locator("[data-note-action='add-child']").click()
    page.wait_for_function("document.querySelector('#accFinancialSetupPanelNotesBalanceSheet')?.textContent.includes('Raw material')")
    assert notes_bs.get_by_text("Raw material").is_visible()
    page.once("dialog", lambda dialog: dialog.accept("Opening stock"))
    notes_bs.locator(".acc2-notes-group.legacy [data-note-action='add']").click()
    page.wait_for_function("document.querySelector('#accFinancialSetupPanelNotesBalanceSheet')?.textContent.includes('Opening stock')")
    raw_material = notes_bs.locator("[data-note-row][data-note-id='22']")
    opening_stock = notes_bs.locator("[data-note-row][data-note-id='23']")
    opening_stock.locator("[data-note-drag-handle]").drag_to(raw_material, target_position={"x": 25, "y": raw_material.bounding_box()["height"] // 2})
    page.wait_for_function("document.querySelector('#accFinancialSetupPanelNotesBalanceSheet [data-note-id=\"22\"] > [data-note-drop-parent] [data-note-id=\"23\"]') !== null")
    assert any(call[0] == "PUT" and call[2].get("category_type") == "note" and call[2].get("parent_category_id") for call in calls)
    page.once("dialog", lambda dialog: dialog.accept("Export Customers"))
    notes_bs.locator("[data-note-source-drop][data-source-id='42'] [data-note-action='add-child']").click()
    page.wait_for_function("document.querySelector('#accFinancialSetupPanelNotesBalanceSheet')?.textContent.includes('Export Customers')")
    page.locator("#accFinancialSetupClose").click()
    assert page.locator("#accFinancialSetupModal").is_hidden()
    assert page.locator("#accFinancialPanel").is_visible()
    assert page.locator("#accVerificationPanel").is_hidden()
    page.locator("#accTabNotes").click()
    assert page.locator("#accNotesPanel").is_visible()
    assert "Define reusable note categories" not in page.locator("#accNotesPanel").text_content()
    assert page.locator("#accNotesCategoryForm, #accNotesCategoryList").count() == 0
    page.wait_for_selector("#accNotesGridWrap:not([hidden])")
    assert page.locator("#accNotesRows .acc2-note-category").count() == 4
    notes_fab = page.locator("#accNotesSetupFab")
    assert notes_fab.is_visible()
    notes_fab.click()
    assert page.locator("#accFinancialSetupModal").is_visible()
    assert page.locator("#accFinancialSetupModal").get_attribute("data-entry-mode") == "notes"
    assert page.locator("#accFinancialSetupModalTitle").text_content() == "Notes to Financial Statements"
    assert page.locator("#accFinancialSetupTabNotesBalanceSheet").get_attribute("aria-selected") == "true"
    for panel_id in ("accFinancialSetupPanelBalanceSheet", "accFinancialSetupPanelIncomeStatement"):
        panel = page.locator("#" + panel_id)
        assert panel.locator("[data-fs-action], [data-fs-drag-handle]").count() == 0
        assert "Read-only reference" in panel.text_content()
    assert page.locator("#accFinancialSetupPanelNotesBalanceSheet [data-note-action]").count() > 0
    page.locator("#accFinancialSetupTabIncomeStatement").click()
    assert page.locator("#accFinancialSetupPanelIncomeStatement").get_attribute("hidden") is None
    assert page.locator("#accFinancialSetupPanelIncomeStatement [data-fs-action], #accFinancialSetupPanelIncomeStatement [data-fs-drag-handle]").count() == 0
    page.locator("#accFinancialSetupClose").click()
    assert page.evaluate("document.activeElement.id") == "accNotesSetupFab"
    page.locator("#accTabFinancial").click()
    page.locator("#accFinancialSetupFab").click()
    assert page.locator("#accFinancialSetupModal").get_attribute("data-entry-mode") == "financial"
    assert page.locator("#accFinancialSetupPanelBalanceSheet [data-fs-action='add-heading']").is_visible()
    balance_sheet = page.locator("#accFinancialSetupPanelBalanceSheet")
    balance_sheet.locator("[data-fs-calc-action='add-subtotal'][data-fs-heading-id='40']").click()
    editor = balance_sheet.locator("[data-fs-calc-editor]")
    editor.locator("[data-fs-calc-label]").fill("Total Non Current Assets")
    editor.locator("[data-source-category-id='42']").locator("xpath=ancestor::label[1]").locator("[data-fs-calc-operator]").select_option("subtract")
    editor.locator("[data-fs-calc-action='save']").click()
    page.wait_for_function("document.querySelector('#accFinancialSetupPanelBalanceSheet')?.textContent.includes('Total Non Current Assets')")
    subtotal = balance_sheet.locator("[data-fs-calculation-id]").filter(has_text="Total Non Current Assets")
    assert "Stock − Cash equivalents" in subtotal.text_content()

    page.once("dialog", lambda dialog: dialog.accept("Non Current Assets"))
    balance_sheet.locator("[data-fs-action='add-heading']").click()
    page.wait_for_function("Array.from(document.querySelectorAll('#accFinancialSetupPanelBalanceSheet .acc2-fs-heading-title strong')).some(heading => heading.textContent === 'Non Current Assets')")
    non_current_heading_id = page.evaluate("""() => Array.from(document.querySelectorAll('#accFinancialSetupPanelBalanceSheet .acc2-fs-heading'))
        .find(heading => heading.querySelector('.acc2-fs-heading-title strong')?.textContent === 'Non Current Assets')?.dataset.fsId""")
    non_current_heading = balance_sheet.locator(".acc2-fs-heading[data-fs-id='{}']".format(non_current_heading_id))
    page.once("dialog", lambda dialog: dialog.accept("Property and Equipment"))
    non_current_heading.locator("[data-fs-action='add-category']").click()
    page.wait_for_function("document.querySelector('#accFinancialSetupPanelBalanceSheet')?.textContent.includes('Property and Equipment')")
    non_current_heading.locator("[data-fs-calc-action='add-subtotal']").click()
    editor = balance_sheet.locator("[data-fs-calc-editor]")
    editor.locator("[data-fs-calc-label]").fill("Total Non Current Assets")
    editor.locator("[data-fs-calc-action='save']").click()
    page.wait_for_function("document.querySelector('#accFinancialSetupPanelBalanceSheet')?.textContent.includes('Total Non Current Assets')")

    page.once("dialog", lambda dialog: dialog.accept("Capital"))
    balance_sheet.locator("[data-fs-action='add-heading']").click()
    page.wait_for_function("document.querySelector('#accFinancialSetupPanelBalanceSheet')?.textContent.includes('Capital')")
    balance_sheet.locator("[data-fs-calc-action='add-total']").click()
    editor = balance_sheet.locator("[data-fs-calc-editor]")
    editor.locator("[data-fs-calc-label]").fill("Total Assets")
    editor.locator("[data-source-calculation-id='101']").check()
    assert editor.locator("[data-fs-calc-placement]").text_content() == "Appears after: Current Assets Heading"
    editor.locator("[data-source-calculation-id='102']").check()
    assert editor.locator("[data-fs-calc-placement]").text_content() == "Appears after: Non Current Assets"
    assert editor.locator("[data-source-category-id='44']").is_disabled()
    editor.locator("[data-source-calculation-id='102']").uncheck()
    editor.locator("[data-source-category-id='44']").check()
    assert editor.locator("[data-source-calculation-id='102']").is_disabled()
    assert editor.locator("[data-fs-calc-placement]").text_content() == "Appears after: Non Current Assets"
    editor.locator("[data-fs-calc-action='save']").click()
    page.wait_for_function("document.querySelector('#accFinancialSetupPanelBalanceSheet')?.textContent.includes('Total Assets')")
    total = balance_sheet.locator("[data-fs-calculation-id]").filter(has_text="Total Assets")
    assert balance_sheet.locator("[data-fs-calculation-id]").count() == 3
    assert total.evaluate("element => element.previousElementSibling.dataset.fsId") == non_current_heading.get_attribute("data-fs-id")
    assert total.evaluate("element => element.nextElementSibling.textContent.includes('Capital')")
    assert any(call[0] == "POST" and call[2].get("calculation_type") == "total" and any(component.get("source_category_id") == 44 for component in call[2].get("components", [])) for call in calls)
    page.locator("#accFinancialSetupTabNotesBalanceSheet").click()
    assert page.locator("#accFinancialSetupPanelNotesBalanceSheet [data-fs-calculation-id]").count() == 0
    page.locator("#accFinancialSetupTabBalanceSheet").click()
    page.locator("#accFinancialSetupClose").click()
    choose_financial_category(page, "20-15-50-0001", 42)
    page.wait_for_function("document.querySelector('#accStatus')?.textContent.includes('20-15-50-0001')")
    choose_financial_category(page, "20-15-50-0002", 42)
    page.wait_for_function("document.querySelector('#accStatus')?.textContent.includes('20-15-50-0002')")
    choose_financial_category(page, "10-15-115-10-0001", 41)
    page.wait_for_function("document.querySelector('#accStatus')?.textContent.includes('10-15-115-10-0001')")
    page.locator("#accFinancialCategoryFilter").fill("Cash equivalents")
    assert page.locator("#accFinancialRows tr").count() == 4
    page.locator("#accFinancialFilterLoad").click()
    financial_filtered_rows = page.locator("#accFinancialRows tr")
    assert financial_filtered_rows.count() == 3
    assert "Local creditors" in financial_filtered_rows.all_text_contents()[0]
    assert all("Cash equivalents" in page.locator("#accFinancialRows tr").nth(index).locator("[data-fs-picker-value-label]").text_content() for index in (0, 1, 2))
    page.locator("#accFinancialFilter").fill("Supplier A")
    assert page.locator("#accFinancialRows tr").count() == 3
    page.locator("#accFinancialFilterLoad").click()
    assert page.locator("#accFinancialRows tr").count() == 2
    assert "Local creditors" in page.locator("#accFinancialRows tr").nth(0).text_content()
    page.locator("#accFinancialFilter").fill("")
    page.locator("#accFinancialCategoryFilter").fill("")
    page.locator("#accFinancialFilterLoad").click()
    page.locator("#accTabNotes").click()
    notes_parent = page.locator("#accNotesRows tr").filter(has_text="Local creditors")
    assert notes_parent.locator(".acc2-note-source").text_content().endswith("Cash equivalents")
    page.once("dialog", lambda dialog: dialog.accept())
    choose_note(page, "20-15-50", 24)
    page.wait_for_function("document.querySelector('#accNotesRows [data-acc-note-result=\"20-15-50-0001\"]')?.textContent === 'Saved'")
    assert any(call[0] == "POST" and call[1].endswith("/mapping/apply-to-children") and call[2].get("note_category_id") == 24 for call in calls)
    supplier_a = page.locator("#accNotesRows tr:has(select.acc2-note-category[data-acc-code='20-15-50-0001'])")
    supplier_a.locator("[data-fs-picker-trigger]").click()
    note_menu = supplier_a.locator("[data-fs-picker-menu]")
    assert note_menu.locator("[data-fs-picker-option] > span:first-child").all_text_contents() == ["Unassigned", "Cash equivalents › Raw material › Opening stock", "Cash equivalents › Export Customers"]
    note_menu.locator("[data-fs-picker-search-input]").fill("Opening stock")
    assert note_menu.locator("[data-fs-picker-option]:visible").count() == 1
    note_menu.locator("[data-fs-picker-search-input]").fill("")
    note_menu.locator("[data-fs-picker-option][data-fs-picker-value='23']").click()
    page.wait_for_function("document.querySelector('#accNotesRows [data-acc-note-result=\"20-15-50-0001\"]')?.textContent === 'Saved'")
    assert any(call[0] == "PUT" and call[2].get("note_category_id") == 23 and "verification_workstream" not in call[2] and "financial_statement_category_id" not in call[2] for call in calls)
    page.locator("#accNotesCategoryFilter").fill("Opening stock")
    assert page.locator("#accNotesRows tr").count() == 4
    page.locator("#accNotesFilterLoad").click()
    note_filtered_rows = page.locator("#accNotesRows tr")
    assert note_filtered_rows.count() == 2
    assert "Local creditors" in note_filtered_rows.nth(0).text_content()
    assert "Supplier A" in note_filtered_rows.nth(1).text_content()
    page.locator("#accNotesCategoryFilter").fill("")
    page.locator("#accNotesFilter").fill("Cash in hand")
    assert page.locator("#accNotesRows tr").count() == 2
    page.locator("#accNotesFilterLoad").click()
    assert page.locator("#accNotesRows tr").count() == 1
    assert "Cash in hand" in page.locator("#accNotesRows tr").first.text_content()
    page.locator("#accNotesFilter").fill("")
    page.locator("#accNotesFilterLoad").click()
    cash_row = page.locator("#accNotesRows tr:has(select.acc2-note-category[data-acc-code='10-15-115-10-0001'])")
    assert cash_row.locator(".acc2-note-source").text_content().endswith("Stock")
    assert cash_row.locator("[data-fs-picker-trigger]").is_disabled()
    assert cash_row.locator("[data-fs-picker-value-label]").text_content() == "No notes defined under this category"
    page.locator("#accTabVerification").click()
    page.locator("#accAccountFilter").fill("Supplier A")
    assert page.locator("#accAccountRows tr").count() == 1
    page.close()


def test_cashflow_setup_uses_categories_and_configuration_popups(browser, static_server):
    accounts = [
        {"id": 1, "account_code": "10-15", "account_name": "Receipts", "parent_account_code": None, "level": 2, "is_leaf": False, "assignment": {"verification_workstream": None}},
        {"id": 2, "account_code": "10-15-0001", "account_name": "Cash account", "parent_account_code": "10-15", "level": 3, "is_leaf": True, "category_id": None},
        {"id": 3, "account_code": "10-15-0002", "account_name": "Bank account", "parent_account_code": "10-15", "level": 3, "is_leaf": True, "category_id": None},
    ]
    categories = [{"id": 1, "client_id": 1, "category_key": "customer_receipts", "label": "Customer receipts", "sort_order": 0, "is_active": True}]
    historical_orphans = [{"account_code": "10-15-0009", "account_name": "Historical cash account", "parent_account_code": "10-15", "transaction_count": 2, "voucher_count": 1, "is_historical_orphan": True, "is_read_only": True, "category_id": None}]
    calls = []
    mapping_gets = []

    def handle_api(route, request):
        url = request.url
        if "/api/auth/current-client" in url:
            route.fulfill(status=200, content_type="application/json", body=json.dumps({"client_id": 1, "client_name": "Regression Client", "client_code": "RC"}))
            return
        if "/api/users" in url:
            route.fulfill(status=200, content_type="application/json", body=json.dumps([]))
            return
        if url.split("?")[0].endswith("/api/account-control-center/mapping"):
            if request.method == "GET":
                mapping_gets.append(request.url)
            route.fulfill(status=200, content_type="application/json", body=json.dumps({
                "client_id": 1,
                "upload": {"id": 77, "file_name": "January Trial Balance.xlsx", "as_of_date": "2026-01-31"},
                "accounts": accounts,
            }))
            return
        base = url.split("?")[0]
        if base.endswith("/api/account-control-center/cashflow/setup"):
            route.fulfill(status=200, content_type="application/json", body=json.dumps({
                "client_id": 1,
                "upload": {"id": 77, "file_name": "January Trial Balance.xlsx", "as_of_date": "2026-01-31"},
                "categories": categories,
                "accounts": accounts,
                "historical_orphans": historical_orphans,
                "treasury_accounts": {"cash": [], "bank": []},
                "settings": {"match_internal_transfers": False, "receipt_presentation": "net", "source_template_id": None, "role_categories": {"customer_receipt": [1], "bank_charge": [], "withholding_tax": [], "other_deduction": []}},
            }))
            return
        if base.endswith("/api/reconciliation/cashflows/sources"):
            route.fulfill(status=200, content_type="application/json", body=json.dumps({
                "client_id": 1,
                "sources": [{"template_id": 1, "template_name": "Transaction Report", "file_name": "January vouchers.xlsx"}],
            }))
            return
        if base.endswith("/api/reconciliation/cashflows/template-columns") and request.method == "GET":
            roles = [
                {"role": "voucher_id", "label": "Voucher ID", "required": True, "template_column_id": 15, "column_name": "Voucher ID"},
                {"role": "date", "label": "Transaction Date", "required": True, "template_column_id": 12, "column_name": "Transaction Date"},
                {"role": "account_code", "label": "Account Detail / Account Code", "required": True, "template_column_id": 16, "column_name": "Account Detail"},
                {"role": "account_name", "label": "Account Name", "required": False, "template_column_id": None, "column_name": None},
                {"role": "voucher_detail", "label": "Voucher Detail", "required": False, "template_column_id": None, "column_name": None},
                {"role": "debit", "label": "Debit Amount", "required": True, "template_column_id": 19, "column_name": "Debit Amount"},
                {"role": "credit", "label": "Credit Amount", "required": True, "template_column_id": 20, "column_name": "Credit Amount"},
            ]
            columns = [{"template_column_id": value, "column_name": name} for value, name in [(15, "Voucher ID"), (12, "Transaction Date"), (16, "Account Detail"), (19, "Debit Amount"), (20, "Credit Amount")]]
            route.fulfill(status=200, content_type="application/json", body=json.dumps({"client_id": 1, "template": {"id": 1, "template_name": "Transaction Report"}, "columns": columns, "roles": roles, "missing_required_roles": [], "ready": True}))
            return
        if base.endswith("/api/reconciliation/cashflows/template-columns") and request.method == "PUT":
            body = json.loads(request.post_data or "{}")
            calls.append((request.method, body))
            route.fulfill(status=200, content_type="application/json", body=json.dumps({"client_id": 1, "template": {"id": 1, "template_name": "Transaction Report"}, "columns": [], "roles": [], "missing_required_roles": [], "ready": True}))
            return
        if base.endswith("/api/account-control-center/cashflow/categories") and request.method == "POST":
            body = json.loads(request.post_data or "{}")
            calls.append((request.method, body))
            categories.append({"id": 2, "client_id": 1, "category_key": body["category_key"], "label": body["label"], "sort_order": 1, "is_active": True})
            route.fulfill(status=201, content_type="application/json", body=json.dumps({"saved": True, "category": categories[-1]}))
            return
        if "/api/account-control-center/cashflow/categories/" in base and request.method == "PUT":
            body = json.loads(request.post_data or "{}")
            category_id = int(base.rsplit("/", 1)[-1])
            category = next(item for item in categories if item["id"] == category_id)
            category.update({key: body[key] for key in ("label", "sort_order", "is_active") if key in body})
            calls.append((request.method, body))
            route.fulfill(status=200, content_type="application/json", body=json.dumps({"saved": True, "category": category}))
            return
        if base.endswith("/api/account-control-center/cashflow/category-assignment/apply-to-children"):
            body = json.loads(request.post_data or "{}")
            calls.append((request.method, body))
            for account in accounts:
                if account["account_code"].startswith(body["parent_account_code"] + "-") or account["account_code"] == body["parent_account_code"]:
                    account["category_id"] = body.get("category_id")
            for orphan in historical_orphans:
                if orphan["parent_account_code"].startswith(body["parent_account_code"]):
                    orphan["category_id"] = body.get("category_id")
            route.fulfill(status=200, content_type="application/json", body=json.dumps({"saved": True, "leaf_count": 3, "historical_orphan_count": 1}))
            return
        if base.endswith("/api/account-control-center/cashflow/category-assignment"):
            body = json.loads(request.post_data or "{}")
            calls.append((request.method, body))
            for account in accounts:
                if account["account_code"] == body.get("account_code"):
                    account["category_id"] = body.get("category_id")
            route.fulfill(status=200, content_type="application/json", body=json.dumps({"saved": True, **body}))
            return
        if base.endswith("/api/account-control-center/cashflow/treasury-accounts") or base.endswith("/api/account-control-center/cashflow/settings"):
            body = json.loads(request.post_data or "{}")
            calls.append((request.method, body))
            route.fulfill(status=200, content_type="application/json", body=json.dumps({"saved": True, **body}))
            return
        route.fulfill(status=200, content_type="application/json", body="{}")

    page = browser.new_page()
    page.add_init_script("""(() => {
        window.__accOriginalPhase3Fetch = window.fetch;
        window.__accPhase3Hold = {};
        window.__accPhase3Release = {};
        const phase3Key = (input, init) => {
            const url = String(input).split('?')[0];
            const method = String((init && init.method) || 'GET').toUpperCase();
            if (method === 'POST' && url.endsWith('/api/account-control-center/cashflow/categories')) return 'categoryCreate';
            if (method === 'PUT' && url.includes('/api/account-control-center/cashflow/categories/')) return 'categoryUpdate';
            if (method === 'PUT' && url.endsWith('/api/account-control-center/cashflow/treasury-accounts')) return 'treasury';
            if (method === 'PUT' && url.endsWith('/api/account-control-center/cashflow/settings')) return 'settings';
            if (method === 'PUT' && url.endsWith('/api/reconciliation/cashflows/template-columns')) return 'template';
            return null;
        };
        window.fetch = (input, init) => {
            const key = phase3Key(input, init);
            if (key && window.__accPhase3Hold[key]) {
                return new Promise((resolve, reject) => {
                    window.__accPhase3Release[key] = () => window.__accOriginalPhase3Fetch(input, init).then(resolve, reject);
                });
            }
            return window.__accOriginalPhase3Fetch(input, init);
        };
    })();""")
    page.route("**/api/**", handle_api)
    page.goto("{}/static/account_control_center.html".format(static_server), wait_until="load")
    page.wait_for_selector("#accAccountRows .acc2-workstream")
    assert len(mapping_gets) == 1
    assert page.locator("#accCashflowPanel").is_hidden()
    page.locator("#accTabCashflow").click()
    page.wait_for_selector("#accCashflowRows .acc2-cashflow-category")
    assert len(mapping_gets) == 1
    assert page.locator("#accCashflowPanel").is_visible()
    assert page.locator("#accVerificationPanel").is_hidden()
    assert page.locator("#accCashflowRows tr").count() == 4
    assert page.locator("#accCashflowRows .acc2-historical-orphan").count() == 1
    assert page.locator("#accCashflowRows .acc2-historical-orphan .acc2-cashflow-category").count() == 1
    assert page.locator("#accCashflowRows .acc2-historical-orphan td").count() == 5
    assert page.locator("#accCashflowRows .acc2-historical-orphan .acc2-type").text_content() == "Historical / orphan"
    page.locator("#accCashflowFilter").fill("unassigned")
    assert page.locator("#accCashflowRows tr").count() == 4
    page.locator("#accCashflowFilter").fill("")
    page.locator("select.acc2-cashflow-category[data-acc-code='10-15-0009']").select_option("1")
    page.wait_for_timeout(150)
    assert any(method == "PUT" and body.get("account_code") == "10-15-0009" and body.get("category_id") == 1 for method, body in calls)
    assert page.locator("#accCashflowOrphanNotice").is_visible()
    assert page.locator("#accCashflowRows .acc2-cashflow-section").count() == 0
    assert page.locator("#accCashflowRows .acc2-cashflow-role").count() == 0
    assert page.locator("#accCashflowRows .acc2-cashflow-treatment").count() == 0
    page.once("dialog", lambda dialog: dialog.accept())
    page.locator("select.acc2-cashflow-category[data-acc-code='10-15']").select_option("1")
    page.wait_for_timeout(200)
    assert any(method == "POST" and body.get("parent_account_code") == "10-15" and body.get("category_id") == 1 for method, body in calls)
    assert page.locator("select.acc2-cashflow-category[data-acc-code='10-15-0001']").input_value() == "1"
    assert page.locator("select.acc2-cashflow-category[data-acc-code='10-15-0002']").input_value() == "1"
    assert page.locator("select.acc2-cashflow-category[data-acc-code='10-15-0009']").input_value() == "1"
    page.locator("#accCashflowFilter").fill("Customer receipts")
    assert page.locator("#accCashflowRows tr").count() == 4
    page.locator("#accCashflowFilter").fill("customer_receipts")
    assert page.locator("#accCashflowRows tr").count() == 4
    page.locator("#accCashflowFilter").fill("")
    page.locator("#accCashflowCategoryFab").click()
    page.locator("#accCashflowCategoryKey").fill("supplier_payments")
    page.locator("#accCashflowCategoryLabel").fill("Supplier payments")
    page.evaluate("window.__accPhase3Hold.categoryCreate = true")
    page.locator("#accCashflowCategoryForm button[type='submit']").click()
    page.locator("#accPageBusy").wait_for(state="visible")
    assert page.locator("#accCashflowCategoryForm button[type='submit']").get_attribute("aria-busy") == "true"
    assert page.locator("#accCashflowCategoryForm button[type='submit'] .ui-spinner").is_visible()
    page.evaluate("window.__accPhase3Release.categoryCreate()")
    page.locator("#accPageBusy").wait_for(state="hidden")
    page.wait_for_timeout(150)
    assert page.locator("#accCashflowCategoryList .acc2-category-item").count() == 2
    page.evaluate("window.__accPhase3Hold.categoryUpdate = true")
    page.locator("[data-acc-cashflow-category-id='2'][data-acc-cashflow-category-action='toggle']").click()
    page.locator("#accPageBusy").wait_for(state="visible")
    assert page.locator("[data-acc-cashflow-category-id='2'][data-acc-cashflow-category-action='toggle']").get_attribute("aria-busy") == "true"
    page.evaluate("window.__accPhase3Release.categoryUpdate()")
    page.locator("#accPageBusy").wait_for(state="hidden")
    page.wait_for_timeout(150)
    page.locator("#accCashflowCategoryClose").click()
    page.locator("select.acc2-cashflow-category[data-acc-code='10-15-0001']").select_option("1")
    page.wait_for_timeout(150)
    assert any(method == "PUT" and body.get("category_id") == 1 for method, body in calls)
    page.locator("#accCashflowAccountsButton").click()
    assert page.locator("#accCashflowCashOptions .acc2-account-parent").count() == 1
    assert page.locator("#accCashflowCashSelected .acc2-treasury-empty").count() == 1
    page.locator("#accCashflowCashSearch").fill("Cash account")
    page.locator("#accCashflowCashOptions input:not(:disabled)").first.check()
    assert page.locator("#accCashflowCashSelected").get_by_text("10-15-0001").count() == 1
    page.locator("#accCashflowAccountsClose").click()
    page.locator("#accTabVerification").click()
    page.locator("#accAccountFilter").fill("Cash account")
    assert page.locator("#accAccountRows tr").count() == 1
    assert len(mapping_gets) == 1
    page.locator("#accTabCashflow").click()
    page.locator("#accCashflowAccountsButton").click()
    page.locator("[data-acc-cashflow-config-tab='bank']").click()
    page.locator("#accCashflowBankSearch").fill("Bank account")
    page.locator("#accCashflowBankOptions input:not(:disabled)").first.check()
    assert page.locator("#accCashflowBankSelected").get_by_text("10-15-0002").count() == 1
    page.evaluate("window.__accPhase3Hold.treasury = true")
    page.locator("#accCashflowAccountsSave").click()
    page.locator("#accPageBusy").wait_for(state="visible")
    assert page.locator("#accCashflowAccountsSave").get_attribute("aria-busy") == "true"
    assert page.locator("#accCashflowAccountsSave .ui-spinner").is_visible()
    page.evaluate("window.__accPhase3Release.treasury()")
    page.locator("#accPageBusy").wait_for(state="hidden")
    page.wait_for_timeout(150)
    assert any(method == "PUT" and "cash_account_codes" in body and "bank_account_codes" in body for method, body in calls)
    page.locator("[data-acc-cashflow-config-tab='transfers']").click()
    assert page.locator("#accCashflowMatchTransfers").is_visible()
    page.evaluate("window.__accPhase3Hold.settings = true")
    page.locator("#accCashflowSettingsSave").click()
    page.locator("#accPageBusy").wait_for(state="visible")
    assert page.locator("#accCashflowSettingsSave").get_attribute("aria-busy") == "true"
    page.evaluate("window.__accPhase3Release.settings()")
    page.locator("#accPageBusy").wait_for(state="hidden")
    page.wait_for_timeout(150)
    page.locator("[data-acc-cashflow-config-tab='customer']").click()
    assert page.locator(".acc2-payment-format-card").count() == 2
    assert page.locator("#accCashflowCustomerReceiptCategories").input_value() == "Customer receipts"
    assert page.locator("#accCashflowCustomerReceiptCategoryOptions option[value='Customer receipts']").count() == 1
    page.locator("#accCashflowCustomerReceiptCategories").fill("Customer receipts")
    page.evaluate("window.__accPhase3Hold.settings = true")
    page.locator("#accCashflowCustomerSettingsSave").click()
    page.locator("#accPageBusy").wait_for(state="visible")
    assert page.locator("#accCashflowCustomerSettingsSave").get_attribute("aria-busy") == "true"
    page.evaluate("window.__accPhase3Release.settings()")
    page.locator("#accPageBusy").wait_for(state="hidden")
    page.wait_for_timeout(150)
    assert any(method == "PUT" and body.get("customer_receipt_category_ids") == [1] for method, body in calls)
    page.locator("[data-acc-cashflow-config-tab='transfers']").click()
    page.locator("#accCashflowAccountsClose").click()
    page.locator("#accCashflowTemplateButton").click()
    page.wait_for_selector("#accCashflowTemplateSelect option[value='1']", state="attached")
    assert page.locator("#accCashflowTemplateModal").is_visible()
    page.wait_for_selector("#accCashflowTemplateMapping .acc2-template-role")
    assert page.locator("#accCashflowTemplateMapping .acc2-template-required").count() == 5
    assert page.locator("#accCashflowTemplateMapping .acc2-template-recommended").count() == 2
    assert page.locator("#accCashflowTemplateSave").is_enabled()
    page.evaluate("window.__accPhase3Hold.template = true")
    page.locator("#accCashflowTemplateSave").click()
    page.locator("#accPageBusy").wait_for(state="visible")
    assert page.locator("#accCashflowTemplateSave").get_attribute("aria-busy") == "true"
    assert page.locator("#accCashflowTemplateSave .ui-spinner").is_visible()
    page.evaluate("window.__accPhase3Release.template()")
    page.locator("#accPageBusy").wait_for(state="hidden")
    page.wait_for_timeout(150)
    assert any(method == "PUT" and body.get("template_id") == 1 and len(body.get("mappings", [])) == 5 for method, body in calls)
    page.locator("#accCashflowTemplateClose").click()
    page.close()
