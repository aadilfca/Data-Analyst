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


def test_verification_grid_loads_saves_and_applies_parent_assignment(browser, static_server):
    mapping = {
        "client_id": 1,
        "upload": {"id": 77, "file_name": "January Trial Balance.xlsx", "as_of_date": "2026-01-31"},
        "verification_workstreams": ["bank", "cash", "local_vendor"],
        "accounts": [
            {"id": 1, "account_code": "20-15-50", "account_name": "Local creditors", "parent_account_code": None, "level": 3, "is_leaf": False, "assignment": {"verification_workstream": None}},
            {"id": 2, "account_code": "20-15-50-0001", "account_name": "Supplier A", "parent_account_code": "20-15-50", "level": 4, "is_leaf": True, "assignment": {"verification_workstream": None}},
            {"id": 3, "account_code": "20-15-50-0002", "account_name": "Supplier B", "parent_account_code": "20-15-50", "level": 4, "is_leaf": True, "assignment": {"verification_workstream": "bank"}},
            {"id": 4, "account_code": "10-15-115-10-0001", "account_name": "Cash in hand", "parent_account_code": None, "level": 5, "is_leaf": True, "assignment": {"verification_workstream": "cash"}},
        ],
    }
    mapping["categories"] = [
        {"id": 10, "client_id": 1, "category_type": "financial_statement", "category_key": "current_assets", "label": "Current assets", "sort_order": 0, "is_active": True},
        {"id": 11, "client_id": 1, "category_type": "financial_statement", "category_key": "current_liabilities", "label": "Current liabilities", "sort_order": 1, "is_active": True},
    ]
    mapping["note_categories"] = [
        {"id": 20, "client_id": 1, "category_type": "note", "category_key": "accounting_policies", "label": "Accounting policies", "sort_order": 0, "is_active": True},
    ]
    calls = []

    def handle_api(route, request):
        url = request.url
        if "/api/auth/current-client" in url:
            route.fulfill(status=200, content_type="application/json", body=json.dumps({"client_id": 1, "client_name": "Regression Client", "client_code": "RC"}))
            return
        if "/api/users" in url:
            route.fulfill(status=200, content_type="application/json", body=json.dumps([]))
            return
        if "/api/account-control-center/categories" in url:
            is_note = "category_type=note" in url
            if request.method == "GET":
                category_list = mapping["note_categories"] if is_note else mapping["categories"]
                route.fulfill(status=200, content_type="application/json", body=json.dumps({"categories": category_list}))
            elif request.method == "POST":
                body = json.loads(request.post_data or "{}")
                is_note = body.get("category_type") == "note"
                category_list = mapping["note_categories"] if is_note else mapping["categories"]
                body["id"] = 21 if is_note else 12
                body["is_active"] = True
                category_list.append(body)
                calls.append((request.method, url, body))
                route.fulfill(status=200, content_type="application/json", body=json.dumps({"saved": True, "category": body}))
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
    page.route("**/api/**", handle_api)
    page.goto("{}/static/account_control_center.html".format(static_server), wait_until="load")
    page.wait_for_selector("#accAccountRows .acc2-workstream")
    assert page.locator("#accSourceInfo").text_content() == "Source: January Trial Balance.xlsx - 2026-01-31"
    assert page.locator("#accAccountRows tr").count() == 4
    assert page.locator("select[data-acc-code='10-15-115-10-0001']").input_value() == "cash"
    assert page.locator("select[data-acc-code='20-15-50'] option:checked").text_content() == "Mixed assignments - choose one"

    page.locator("select[data-acc-code='10-15-115-10-0001']").select_option("local_vendor")
    page.wait_for_timeout(150)
    assert any(call[0] == "PUT" and call[2]["verification_workstream"] == "local_vendor" for call in calls)

    page.once("dialog", lambda dialog: dialog.accept())
    page.locator("select[data-acc-code='20-15-50']").select_option("local_vendor")
    page.wait_for_timeout(200)
    assert any(call[0] == "POST" and call[2]["parent_account_code"] == "20-15-50" for call in calls)
    assert page.locator("select[data-acc-code='20-15-50']").input_value() == "local_vendor"

    page.locator("#accTabFinancial").click()
    page.wait_for_selector("#accFinancialRows .acc2-financial-category")
    assert page.locator("#accFinancialCategoryList .acc2-category-item").count() == 2
    assert page.get_by_role("button", name="Rename").count() == 2
    page.locator("select.acc2-financial-category[data-acc-code='20-15-50-0001']").select_option("10")
    page.wait_for_timeout(150)
    assert any(call[0] == "PUT" and call[2].get("financial_statement_category_id") == 10 and "verification_workstream" not in call[2] for call in calls)
    page.locator("#accFinancialCategoryKey").fill("fixed_assets")
    page.locator("#accFinancialCategoryLabel").fill("Fixed assets")
    page.locator("#accFinancialCategoryForm button[type='submit']").click()
    page.wait_for_timeout(150)
    assert page.locator("#accFinancialCategoryList .acc2-category-item").count() == 3
    assert page.locator("#accFinancialPanel").is_visible()
    assert page.locator("#accVerificationPanel").is_hidden()
    page.locator("#accTabNotes").click()
    page.wait_for_selector("#accNotesRows .acc2-note-category")
    assert page.locator("#accNotesCategoryList .acc2-category-item").count() == 1
    page.locator("select.acc2-note-category[data-acc-code='20-15-50-0001']").select_option("20")
    page.wait_for_timeout(150)
    assert any(call[0] == "PUT" and call[2].get("note_category_id") == 20 and "verification_workstream" not in call[2] and "financial_statement_category_id" not in call[2] for call in calls)
    page.locator("#accNotesCategoryKey").fill("estimates")
    page.locator("#accNotesCategoryLabel").fill("Key estimates")
    page.locator("#accNotesCategoryForm button[type='submit']").click()
    page.wait_for_timeout(150)
    assert page.locator("#accNotesCategoryList .acc2-category-item").count() == 2
    assert page.locator("#accNotesPanel").is_visible()
    page.locator("#accTabVerification").click()
    page.locator("#accAccountFilter").fill("Supplier A")
    assert page.locator("#accAccountRows tr").count() == 1
    page.close()
