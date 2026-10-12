"""Hermetic browser coverage for the Data Bank session cache."""

import functools
import json
import os
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

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


def _preview_response(page_number, limit, search=None):
    row_number = page_number
    return {
        "template_id": 1,
        "template_name": "Invoices",
        "total_rows": 26 if not search else 1,
        "sunday_count": 0,
        "page": page_number,
        "limit": limit,
        "document_key_column": "Document No",
        "date_column": "Date",
        "amount_columns": ["Amount"],
        "rows": [{
            "id": row_number,
            "row_data": {
                "Document No": "INV-{}".format(row_number),
                "Date": "2026-01-{:02d}".format(row_number),
                "Amount": row_number * 100,
            },
            "extract_amount": row_number * 100,
            "extract_date": "2026-01-{:02d}".format(row_number),
            "data_quality_flag": None,
            "rate_source": None,
            "document_group_id": "group-{}".format(row_number),
        }],
    }


def open_databank(browser, static_server):
    page = browser.new_page()
    calls = []

    def handle_api(route, request):
        parsed = urlparse(request.url)
        path = parsed.path
        query = parse_qs(parsed.query)

        if path.endswith("/api/auth/current-client"):
            route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps({"client_id": 1, "client_name": "Cache Client", "client_code": "CC"}),
            )
            return
        if path.endswith("/api/settings"):
            route.fulfill(status=200, content_type="application/json", body=json.dumps({}))
            return
        if path.endswith("/api/data/availability"):
            route.fulfill(status=200, content_type="application/json", body=json.dumps([{
                "template_id": 1,
                "template_name": "Invoices",
                "filtered_row_count": 3,
                "total_row_count": 3,
                "document_count": 3,
                "date_min": "2026-01-01",
                "date_max": "2026-01-03",
                "filtered_sunday_count": 0,
                "status": "complete",
                "filtered_status": "complete",
                "estimated_size_formatted": "1 KB",
                "upload_frequency": "Daily",
            }]))
            return
        if path.endswith("/api/data/db-size"):
            route.fulfill(status=200, content_type="application/json", body=json.dumps({"formatted": "1 KB"}))
            return
        if path.endswith("/api/data/overdue-uploads"):
            route.fulfill(status=200, content_type="application/json", body=json.dumps({"overdue": []}))
            return
        if path.endswith("/api/templates/1"):
            route.fulfill(status=200, content_type="application/json", body=json.dumps({
                "id": 1,
                "template_name": "Invoices",
                "has_subtotals_by_default": False,
                "columns": [
                    {"column_name": "Document No", "data_type": "Text"},
                    {"column_name": "Date", "data_type": "Date"},
                    {"column_name": "Amount", "data_type": "Numeric"},
                ],
            }))
            return
        if path.endswith("/api/data/preview"):
            page_number = int(query.get("page", ["1"])[0])
            limit = int(query.get("limit", ["50"])[0])
            search = query.get("search", [None])[0]
            calls.append({"page": page_number, "search": search})
            route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps(_preview_response(page_number, limit, search)),
            )
            return

        route.fulfill(status=200, content_type="application/json", body=json.dumps({}))

    page.route("**/api/**", handle_api)
    page.goto("{}/static/databank.html".format(static_server), wait_until="load")
    page.wait_for_selector("#yourDatasetsBody tr")
    return page, calls


def preview_calls(calls, page_number, search=None):
    return [call for call in calls if call["page"] == page_number and call["search"] == search]


def test_databank_loads_each_scope_once_and_reuses_preview_pages(browser, static_server):
    page, calls = open_databank(browser, static_server)
    try:
        availability_before = page.evaluate(
            "() => performance.getEntriesByType('resource').filter(entry => entry.name.includes('/api/data/availability')).length"
        )
        page.evaluate("() => fetchAvailability()")
        availability_after = page.evaluate(
            "() => performance.getEntriesByType('resource').filter(entry => entry.name.includes('/api/data/availability')).length"
        )
        assert availability_after == availability_before

        page.evaluate("() => { pageSizeSelect.value = '25'; return openPreview(1, 'Invoices'); }")
        page.wait_for_selector("#previewModal", state="visible")
        page.wait_for_function("() => document.body.innerText.includes('INV-1')")
        page.wait_for_timeout(100)

        assert len(preview_calls(calls, 1)) == 1
        assert len(preview_calls(calls, 2)) == 1, "the next page should be prefetched once"

        page.locator("#nextPageBtn").click()
        page.wait_for_function("() => document.body.innerText.includes('INV-2')")
        assert len(preview_calls(calls, 2)) == 1

        page.evaluate("() => { previewModal.style.display = 'none'; isPreviewOpen = false; return openPreview(1, 'Invoices'); }")
        page.wait_for_function("() => document.body.innerText.includes('INV-1')")
        assert len(preview_calls(calls, 1)) == 1

        page.evaluate("() => { searchInput.value = 'new search'; applyPreviewFilters(); }")
        page.wait_for_function("() => document.body.innerText.includes('INV-1')")
        assert len(preview_calls(calls, 1, "new search")) == 1
    finally:
        page.close()
