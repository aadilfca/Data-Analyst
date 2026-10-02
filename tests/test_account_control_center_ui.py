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
    assert page.locator(".acc2-header").count() == 0
    assert page.locator("#accStatus").is_hidden()
    page.wait_for_selector("#accAccountRows .acc2-workstream")
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
    page.locator("#accCashflowCategoryFab").click()
    page.locator("#accCashflowCategoryKey").fill("supplier_payments")
    page.locator("#accCashflowCategoryLabel").fill("Supplier payments")
    page.locator("#accCashflowCategoryForm button[type='submit']").click()
    page.wait_for_timeout(150)
    assert page.locator("#accCashflowCategoryList .acc2-category-item").count() == 2
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
    page.locator("#accCashflowAccountsSave").click()
    page.wait_for_timeout(150)
    assert any(method == "PUT" and "cash_account_codes" in body and "bank_account_codes" in body for method, body in calls)
    page.locator("[data-acc-cashflow-config-tab='transfers']").click()
    assert page.locator("#accCashflowMatchTransfers").is_visible()
    page.locator("[data-acc-cashflow-config-tab='customer']").click()
    assert page.locator(".acc2-payment-format-card").count() == 2
    assert page.locator("#accCashflowCustomerReceiptCategories").input_value() == "Customer receipts"
    assert page.locator("#accCashflowCustomerReceiptCategoryOptions option[value='Customer receipts']").count() == 1
    page.locator("#accCashflowCustomerReceiptCategories").fill("Customer receipts")
    page.locator("#accCashflowCustomerSettingsSave").click()
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
    page.locator("#accCashflowTemplateSave").click()
    page.wait_for_timeout(150)
    assert any(method == "PUT" and body.get("template_id") == 1 and len(body.get("mappings", [])) == 5 for method, body in calls)
    page.locator("#accCashflowTemplateClose").click()
    page.close()
