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
        assert "Data check unavailable. Try again." in page.locator("#integrityMessage").inner_text()
        assert page.locator("#integrityTableBody").inner_text() == "Data check unavailable. Try again."
    finally:
        page.close()


def test_integrity_page_table_scrolls_on_mobile_without_page_overflow(browser, static_server):
    page, _ = open_integrity_page(browser, static_server)
    try:
        page.wait_for_function("() => document.querySelectorAll('#integrityTableBody tr').length === 3")
        page.set_viewport_size({"width": 360, "height": 800})
        metrics = page.locator(".integrity-table-wrap").evaluate(
            "el => ({width:el.clientWidth, scrollWidth:el.scrollWidth, bodyWidth:document.body.scrollWidth})"
        )
        assert metrics["scrollWidth"] > metrics["width"]
        assert metrics["bodyWidth"] <= 360
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
