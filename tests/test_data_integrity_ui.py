"""Hermetic Playwright coverage for the Data Integrity page and dashboard summary."""

import functools
import json
import os
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

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
            instance = playwright.chromium.launch()
        except Exception as exc:
            pytest.skip("Chromium not available for Playwright: {}".format(exc))
        try:
            yield instance
        finally:
            instance.close()


def _overview(status="Review; scan incomplete"):
    return {
        "client_id": 5,
        "client_name": "Leather Crop",
        "scanned_at": "2026-11-02T12:00:00+00:00",
        "overall_status": status,
        "modules": [
            {
                "module": "Client Data",
                "row_count": 126180,
                "data_bytes": 71914715,
                "data_size": "68.6 MB",
                "associated_record_count": 56,
                "associated_data_bytes": 1000,
                "associated_data_size": "1.0 KB",
                "total_data_bytes": 71915715,
                "total_data_size": "68.6 MB",
                "size_status": "Incomplete",
                "duplicate_groups": 0,
                "duplicate_extra_rows": 0,
                "duplicate_status": "Clear",
                "duplicate_basis": "Active rows, grouped by template and saved source identity.",
                "unidentified_rows": 0,
                "unmeasured_source_files": 27,
            },
            {
                "module": "Trial Balance",
                "row_count": 1488,
                "data_bytes": 520872,
                "data_size": "508.7 KB",
                "associated_record_count": 1,
                "associated_data_bytes": 162,
                "associated_data_size": "162 B",
                "total_data_bytes": 521034,
                "total_data_size": "508.8 KB",
                "size_status": "Estimated",
                "duplicate_groups": 1,
                "duplicate_extra_rows": 1,
                "duplicate_status": "Review",
                "duplicate_basis": "Account codes are compared only within the same Trial Balance upload.",
                "unidentified_rows": 0,
                "unmeasured_source_files": 0,
            },
            {
                "module": "Reconciliation",
                "row_count": 0,
                "data_bytes": 0,
                "data_size": "0 B",
                "associated_record_count": 0,
                "associated_data_bytes": 0,
                "associated_data_size": "0 B",
                "total_data_bytes": 0,
                "total_data_size": "0 B",
                "size_status": "Estimated",
                "duplicate_groups": 0,
                "duplicate_extra_rows": 0,
                "duplicate_status": "Clear",
                "duplicate_basis": "Balance inputs are compared by group, account code, and month.",
                "unidentified_rows": 0,
                "unmeasured_source_files": 0,
            },
        ],
        "database_context": {
            "database_bytes": 268651667,
            "database_size": "256.2 MB",
            "shared_index_bytes": 120725504,
            "shared_index_size": "115.1 MB",
        },
        "size_note": "Sizes are estimates. Shared indexes are shown separately.",
        "scope_note": "Matching IDs across modules are treated as separate data.",
    }


def _safeguards(status='Passed'):
    return {
        'status': status,
        'scope_note': 'Shared structures; record checks cover this client only.',
        'limit_note': 'Does not inspect every physical index entry or measure speed or storage.',
        'modules': [
            {'module': name, 'row_count': 0 if name == 'Reconciliation' else 4,
             'status': status,
             'checks': [
                 {'label': label, 'status': status,
                  'detail': 'No records to check.' if name == 'Reconciliation' and label == 'Record identification'
                  else 'Missing, different or unavailable: Duplicate prevention' if status == 'Needs attention'
                  else 'Check unavailable.' if status == 'Not checked' else 'Required check passed.'}
                 for label in ('Required indexes', 'Record identification', 'Duplicate check')],
             } for name in ('Client Data', 'Trial Balance', 'Reconciliation')],
    }


def open_integrity_page(browser, static_server, *, response_status=200, overview=None):
    page = browser.new_page(viewport={"width": 1280, "height": 900})
    calls = []
    body = overview or _overview()

    def handle_api(route, request):
        path = urlparse(request.url).path
        if path.endswith("/api/auth/current-client"):
            route.fulfill(status=200, json={"client_id": 5, "client_name": "Leather Crop", "client_code": "LC001"})
        elif path.endswith("/api/data-integrity/overview"):
            calls.append(request.url)
            route.fulfill(status=response_status, json=body if response_status == 200 else {"detail": "unavailable"})
        else:
            route.fulfill(status=200, json={})

    page.route("**/api/**", handle_api)
    page.goto("{}/static/data_integrity.html".format(static_server), wait_until="load")
    return page, calls


def test_integrity_page_renders_module_counts_sizes_and_duplicate_status(browser, static_server):
    page, calls = open_integrity_page(browser, static_server)
    try:
        page.wait_for_function("() => document.querySelectorAll('#integrityTableBody tr').length === 3")
        page.get_by_role('tab', name='Module inventory', exact=True).click()
        assert "Leather Crop" in page.locator("#integrityClientLabel").inner_text()
        assert page.locator("#integrityOverallStatus").inner_text() == "Review · incomplete"
        rows = page.locator("#integrityTableBody tr")
        assert rows.nth(0).locator("td").count() == 6
        assert "68.6 MB" in rows.nth(0).inner_text()
        assert "126,180" in rows.nth(0).inner_text()
        assert "Clear · 0 groups" in rows.nth(0).inner_text()
        assert "27 source files unavailable" in rows.nth(0).inner_text()
        assert "508.8 KB" in rows.nth(1).inner_text()
        assert "Review · 1 group / 1 extra" in rows.nth(1).inner_text()
        assert "Whole database: 256.2 MB" in page.locator("#integrityDatabaseContext").inner_text()
        assert len(calls) == 1
    finally:
        page.close()


def test_integrity_page_refresh_rechecks_and_keeps_safe_text(browser, static_server):
    malicious = _overview("Review")
    malicious["modules"][0]["module"] = "<img src=x onerror=alert(1)>"
    page, calls = open_integrity_page(browser, static_server, overview=malicious)
    try:
        page.wait_for_function("() => document.querySelectorAll('#integrityTableBody tr').length === 3")
        page.get_by_role('tab', name='Module inventory', exact=True).click()
        assert page.locator("#integrityTableBody img").count() == 0
        assert "<img src=x onerror=alert(1)>" in page.locator("#integrityTableBody").inner_text()
        page.locator("#refreshIntegrityBtn").click()
        page.wait_for_function("() => document.querySelector('#refreshIntegrityBtn').disabled === false")
        assert len(calls) == 2
    finally:
        page.close()


def test_integrity_page_shows_retry_message_for_api_failure(browser, static_server):
    page, _ = open_integrity_page(browser, static_server, response_status=503)
    try:
        page.wait_for_function("() => document.querySelector('#integrityOverallStatus').textContent === 'Unavailable'")
        assert page.locator('#safeguardsOverallStatus').inner_text() == 'Not checked'
        assert page.locator('#safeguardsModules').get_by_text('Not checked', exact=True).count() == 12
        page.get_by_role('tab', name='Module inventory', exact=True).click()
        assert "Data check unavailable. Try again." in page.locator("#integrityMessage").inner_text()
        assert page.locator("#integrityTableBody").inner_text() == "Data check unavailable. Try again."
    finally:
        page.close()


def test_integrity_page_table_scrolls_on_mobile_without_page_overflow(browser, static_server):
    page, _ = open_integrity_page(browser, static_server)
    try:
        page.wait_for_function("() => document.querySelectorAll('#integrityTableBody tr').length === 3")
        page.get_by_role('tab', name='Module inventory', exact=True).click()
        page.set_viewport_size({"width": 360, "height": 800})
        metrics = page.locator(".integrity-table-wrap").evaluate(
            "el => ({width:el.clientWidth, scrollWidth:el.scrollWidth, bodyWidth:document.body.scrollWidth})"
        )
        assert metrics["scrollWidth"] > metrics["width"]
        assert metrics["bodyWidth"] <= 360
    finally:
        page.close()


@pytest.mark.parametrize('status', ['Passed', 'Needs attention', 'Not checked'])
def test_safeguards_default_tab_status_details_and_empty_module(browser, static_server, status):
    overview = _overview()
    overview['safeguards'] = _safeguards(status)
    page, calls = open_integrity_page(browser, static_server, overview=overview)
    try:
        page.wait_for_function('() => document.querySelectorAll(".safeguard-module").length === 3')
        assert page.locator('#safeguardsPanel').is_visible()
        assert not page.locator('#inventoryPanel').is_visible()
        assert page.locator('#safeguardsOverallStatus').inner_text() == status
        assert page.locator('.safeguard-check').count() == 9
        assert 'No records for this client.' in page.locator('.safeguard-module').nth(2).inner_text()
        page.locator('summary').click()
        assert page.locator('#safeguardsScopeNote').is_visible()
        assert 'physical index entry' in page.locator('#safeguardsLimitNote').inner_text()
        assert len(calls) == 1
        assert page.locator('button').filter(has_text='Repair').count() == 0
    finally:
        page.close()


@pytest.mark.parametrize('width', [320, 390, 768])
def test_safeguards_mobile_layout_and_keyboard_tabs(browser, static_server, width):
    overview = _overview()
    overview['safeguards'] = _safeguards()
    page, _ = open_integrity_page(browser, static_server, overview=overview)
    try:
        page.wait_for_function('() => document.querySelector("#safeguardsOverallStatus").textContent === "Passed"')
        page.set_viewport_size({'width': width, 'height': 850})
        assert page.evaluate('document.documentElement.scrollWidth') <= width
        assert page.locator('.navbar').evaluate('''nav => {
            const bounds = nav.getBoundingClientRect();
            return ['#navLeft', '#clientControl', '.nav-user'].every(selector => {
                const child = document.querySelector(selector).getBoundingClientRect();
                return child.top >= bounds.top && child.bottom <= bounds.bottom;
            });
        }''')
        page.locator('#safeguardsTab').focus()
        page.keyboard.press('ArrowRight')
        assert page.locator('#inventoryTab').get_attribute('aria-selected') == 'true'
        assert page.locator('#inventoryPanel').is_visible()
        assert 'feature=module-inventory' in page.url
        page.keyboard.press('Home')
        assert page.locator('#safeguardsTab').get_attribute('aria-selected') == 'true'
        assert page.locator('#safeguardsPanel').is_visible()
    finally:
        page.close()


@pytest.mark.parametrize('missing', ['safeguards', 'module', 'check', 'status'])
def test_missing_safeguard_evidence_never_shows_overall_passed(browser, static_server, missing):
    overview = _overview()
    if missing != 'safeguards':
        overview['safeguards'] = _safeguards()
        if missing == 'module':
            overview['safeguards']['modules'].pop()
        elif missing == 'check':
            overview['safeguards']['modules'][0]['checks'].pop()
        else:
            overview['safeguards']['modules'][0]['checks'][0]['status'] = 'unknown'
    page, _ = open_integrity_page(browser, static_server, overview=overview)
    try:
        page.wait_for_function('() => document.querySelectorAll(".safeguard-module").length === 3')
        assert page.locator('#safeguardsOverallStatus').inner_text() == 'Not checked'
    finally:
        page.close()


def test_refresh_clears_previous_success_and_failure_keeps_not_checked(browser, static_server):
    overview = _overview()
    overview['safeguards'] = _safeguards()
    page, _ = open_integrity_page(browser, static_server, overview=overview)
    try:
        page.wait_for_function('() => document.querySelector("#safeguardsOverallStatus").textContent === "Passed"')
        page.route('**/api/data-integrity/overview?*', lambda route: route.fulfill(status=403, json={'detail': 'unavailable'}))
        page.locator('#refreshIntegrityBtn').click()
        page.wait_for_function('() => document.querySelector("#integrityOverallStatus").textContent === "Unavailable"')
        assert page.locator('#safeguardsOverallStatus').inner_text() == 'Not checked'
        assert page.locator('#safeguardsModules').get_by_text('Passed', exact=True).count() == 0
        assert page.locator('#refreshIntegrityBtn').is_enabled()
    finally:
        page.close()


def test_search_registration_deep_links_and_safe_safeguard_text(browser, static_server):
    overview = _overview()
    overview['safeguards'] = _safeguards('Needs attention')
    overview['safeguards']['modules'][0]['checks'][0]['detail'] = '<img src=x onerror=alert(1)>'
    page, _ = open_integrity_page(browser, static_server, overview=overview)
    try:
        page.wait_for_function('() => document.querySelectorAll(".safeguard-module").length === 3')
        assert page.locator('#safeguardsModules img').count() == 0
        assert '<img src=x onerror=alert(1)>' in page.locator('#safeguardsModules').inner_text()
        entries = page.evaluate("flattenNavManifest().filter(item => item.url && item.url.includes('data_integrity.html'))")
        assert {entry['label'] for entry in entries} == {'Database safeguards', 'Module inventory'}
        page.goto(static_server + '/static/data_integrity.html?feature=module-inventory')
        assert page.locator('#inventoryPanel').is_visible()
        assert not page.locator('#safeguardsPanel').is_visible()
    finally:
        page.close()


def test_scope_mismatch_cannot_render_another_clients_success(browser, static_server):
    overview = _overview()
    overview['client_id'] = 99
    overview['safeguards'] = _safeguards()
    page, _ = open_integrity_page(browser, static_server, overview=overview)
    try:
        page.wait_for_function('() => document.querySelector("#integrityOverallStatus").textContent === "Unavailable"')
        assert page.locator('#safeguardsOverallStatus').inner_text() == 'Not checked'
        assert page.locator('#safeguardsModules').get_by_text('Passed', exact=True).count() == 0
    finally:
        page.close()


def test_dashboard_links_to_integrity_and_loads_selected_client_status(browser, static_server):
    page = browser.new_page(viewport={"width": 1280, "height": 900})
    calls = []

    def handle_api(route, request):
        path = urlparse(request.url).path
        if path.endswith("/api/auth/current-client"):
            route.fulfill(status=200, json={"client_id": 5, "client_name": "Leather Crop", "client_code": "LC001"})
        elif path.endswith("/api/data-integrity/overview"):
            calls.append(request.url)
            route.fulfill(status=200, json=_overview("Clear"))
        elif path.endswith("/api/dashboard/stats"):
            route.fulfill(status=200, json={"last_upload": None})
        elif path.endswith("/api/dashboard/kpi-stats"):
            route.fulfill(status=200, json={"open_queries": 0, "verification_progress_pct": 0, "unresolved_errors": 0})
        elif path.endswith("/api/dashboard/db-size"):
            route.fulfill(status=200, json={"formatted": "256.2 MB"})
        elif path.endswith("/api/dashboard/overdue-uploads"):
            route.fulfill(status=200, json={"overdue": []})
        else:
            route.fulfill(status=200, json={})

    page.route("**/api/**", handle_api)
    try:
        page.goto("{}/static/index.html".format(static_server), wait_until="load")
        page.wait_for_function("() => document.querySelector('#dataIntegrityStatus').textContent === 'Clear'")
        card = page.locator("#dataIntegrityCard")
        assert card.get_attribute("href") == "/static/data_integrity.html"
        assert "client_id=5" in calls[0]
    finally:
        page.close()
