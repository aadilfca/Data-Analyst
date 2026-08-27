"""Regression test for the Verification voucher-navigation skeleton bug.

moveToAdjacentVoucher() (static/verification.js) paints a loading skeleton into
#voucherLinesContainer before fetching the adjacent voucher. Every one of its non-success
exits used to return without clearing that skeleton, so the voucher table was left
shimmering forever and the only way out was a page refresh. This test forces each of those
failure paths in a real browser and asserts the skeleton is gone and a recovery action is
offered instead.

Deliberately hermetic — no dev server, no database, no login. A plain static file server
hands the browser the real static/ pages, and every /api/ call the page makes is stubbed by
Playwright's request interception. Follows the same isolation discipline as the other tests
here: nothing touches the real test.db.
"""

import functools
import json
import os
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

import pytest

# Skips the whole module rather than failing when Playwright isn't installed (it lives in
# requirements-dev.txt, deliberately kept out of the production requirements).
pytest.importorskip("playwright.sync_api")

from playwright.sync_api import sync_playwright  # noqa: E402  (import follows the skip guard)

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# One voucher line, shaped the way /voucher_nav returns them, used to put a real rendered
# voucher on screen before a navigation is attempted.
SEED_VOUCHER = {
    "voucher_id": "CPV-001",
    "voucher_date": "01/04/2026",
    "lines": [
        {
            "detail_id": 9001,
            "client_data_id": 5001,
            "validation_status": "Pending",
            "extract_date": "01/04/2026",
            "row_data": {
                "Account Code": "1001",
                "Account Name": "Office Rent",
                "Description": "April rent",
                "Debit Amount": 25000,
                "Credit Amount": 0,
            },
            "line_items": [],
            "procedure_data": {},
        }
    ],
}


# The voucher a successful navigation lands on — second (and last) of the two-voucher session
# seeded by seed_two_voucher_session(), so afterwards position says Prev is allowed and Next is not.
NEXT_VOUCHER = {
    "voucher_id": "CPV-002",
    "voucher_date": "02/04/2026",
    "lines": [dict(SEED_VOUCHER["lines"][0], detail_id=9002)],
}


class _QuietHandler(SimpleHTTPRequestHandler):
    """SimpleHTTPRequestHandler without the per-request stderr logging."""

    def log_message(self, *args):
        pass


@pytest.fixture(scope="module")
def static_server():
    """Serves the project root so /static/verification.html and its absolute /static/... asset
    paths resolve exactly as they do under the real app."""
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
        except Exception as exc:  # browser binaries not installed on this machine
            pytest.skip("Chromium not available for Playwright: {}".format(exc))
        try:
            yield instance
        finally:
            instance.close()


def open_verification_page(browser, base_url):
    """Opens verification.html with every /api/ call stubbed.

    Returns (page, mode, calls): `mode` is a one-key dict the test mutates to choose how the
    next /voucher_nav call behaves; `calls` accumulates every /voucher_nav URL requested.
    """
    page = browser.new_page()
    mode = {"value": "http_error"}
    calls = []

    def handle_api(route, request):
        url = request.url
        # A valid session, so client_context.js resolves window.clientContextReady normally
        # instead of bouncing the page to login.html.
        if "/api/auth/current-client" in url:
            route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps({"client_id": 1, "client_name": "Regression Client", "client_code": "RC"}),
            )
            return
        if "/api/verification/voucher_nav/" in url:
            calls.append(url)
            if mode["value"] == "http_error":
                route.fulfill(status=500, content_type="application/json", body=json.dumps({"detail": "boom"}))
            elif mode["value"] == "network_error":
                route.abort()
            elif mode["value"] == "boundary":
                route.fulfill(status=200, content_type="application/json", body=json.dumps({"boundary": True}))
            elif mode["value"] == "success":
                route.fulfill(status=200, content_type="application/json", body=json.dumps(NEXT_VOUCHER))
            else:  # "no_lines" — a 200 the render step can't use
                route.fulfill(status=200, content_type="application/json", body=json.dumps({"voucher_id": "X", "lines": []}))
            return
        # Everything else the page happens to call on load.
        route.fulfill(status=200, content_type="application/json", body="{}")

    # API_BASE is '' (same-origin), so this one pattern catches every call the page makes.
    page.route("**/api/**", handle_api)
    page.goto("{}/static/verification.html".format(base_url), wait_until="load")
    page.wait_for_function("typeof moveToAdjacentVoucher === 'function'")
    return page, mode, calls


def seed_rendered_voucher(page):
    """Puts a real rendered voucher on screen through the page's own render pipeline, so a
    navigation attempted afterwards starts from the same state a working session would."""
    page.evaluate("() => { document.getElementById('verificationSessionContent').style.display = 'block'; }")
    page.evaluate(
        """(seed) => {
            currentSessionId = 777;
            currentDetailId = seed.lines[0].detail_id;
            applyVoucherNavRenderData(seed);
        }""",
        SEED_VOUCHER,
    )
    page.wait_for_selector("#voucherLinesContainer .voucher-line-row")


def seed_two_voucher_session(page):
    """Seeds a session of two vouchers, positioned on the first one.

    getVoucherPosition() reads sessionTransactions, so the Prev/Next buttons only have a meaningful
    position rule once it's populated: here that means Prev disallowed (voucher 1 of 2) and Next
    allowed. cleared_status 'Cleared' makes isCurrentVoucherValidated() true so the navigation
    doesn't stop at the "not validated" confirm dialog.
    """
    page.evaluate("() => { document.getElementById('verificationSessionContent').style.display = 'block'; }")
    page.evaluate(
        """(seed) => {
            sessionTransactions = [
                { detail_id: 9001, voucher_id: 'CPV-001', cleared_status: 'Cleared', validation_status: 'Passed' },
                { detail_id: 9002, voucher_id: 'CPV-002', cleared_status: 'Cleared', validation_status: 'Passed' }
            ];
            totalTransactions = 2;
            currentSessionId = 777;
            currentDetailId = seed.lines[0].detail_id;
            applyVoucherNavRenderData(seed);
        }""",
        SEED_VOUCHER,
    )
    page.wait_for_selector("#voucherLinesContainer .voucher-line-row")


def nav_button_state(page):
    return page.evaluate(
        """() => ({
            prev: document.getElementById('prevArrow').disabled,
            next: document.getElementById('nextArrow').disabled,
            badge: !!document.querySelector('.voucher-loading-badge')
        })"""
    )


def start_nav_and_snapshot(page):
    """Kicks off goToNextTransaction() and reads the button state in the SAME evaluate.

    No sleeps or delayed responses needed: JS is single-threaded, and runGuardedVoucherNav()
    disables the buttons and shows the badge synchronously before the first await is reached. So
    the snapshot taken immediately after the call is exactly the state a user sees the instant
    their click returns — which is the guarantee under test.
    """
    return page.evaluate(
        """() => {
            window.__navPromise = goToNextTransaction();
            return {
                prev: document.getElementById('prevArrow').disabled,
                next: document.getElementById('nextArrow').disabled,
                badge: (document.querySelector('.voucher-loading-badge') || {}).textContent || ''
            };
        }"""
    )


def skeleton_row_count(page):
    return page.eval_on_selector_all("#voucherLinesContainer .ui-skeleton-row", "rows => rows.length")


@pytest.mark.parametrize("failure_mode", ["http_error", "network_error", "no_lines"])
def test_failed_voucher_nav_clears_skeleton_and_offers_retry(browser, static_server, failure_mode):
    """The bug itself: a failed navigation must not leave the table shimmering."""
    page, mode, calls = open_verification_page(browser, static_server)
    try:
        seed_rendered_voucher(page)
        mode["value"] = failure_mode

        moved = page.evaluate("() => moveToAdjacentVoucher('next')")
        assert moved is False

        assert skeleton_row_count(page) == 0, "loading skeleton was left behind after a failed navigation"
        error_block = page.locator("#voucherLinesContainer .voucher-nav-error")
        assert error_block.count() == 1
        assert error_block.is_visible()
        assert "next voucher" in error_block.inner_text().lower()
        assert page.locator("#voucherNavRetryBtn").is_visible()
        assert page.locator("#voucherNavReloadBtn").is_visible()
    finally:
        page.close()


def test_retry_button_reissues_the_navigation(browser, static_server):
    """The recovery action has to actually work — and recover fully once the call succeeds."""
    page, mode, calls = open_verification_page(browser, static_server)
    try:
        seed_rendered_voucher(page)
        mode["value"] = "http_error"
        page.evaluate("() => moveToAdjacentVoucher('next')")
        calls_before_retry = len(calls)
        assert calls_before_retry > 0

        # Let the retry succeed, so this also proves the error state isn't a dead end.
        next_voucher = {
            "voucher_id": "CPV-002",
            "voucher_date": "02/04/2026",
            "lines": [dict(SEED_VOUCHER["lines"][0], detail_id=9002)],
        }
        page.unroute("**/api/**")

        def handle_api(route, request):
            if "/api/verification/voucher_nav/" in request.url:
                calls.append(request.url)
                route.fulfill(status=200, content_type="application/json", body=json.dumps(next_voucher))
                return
            route.fulfill(status=200, content_type="application/json", body="{}")

        page.route("**/api/**", handle_api)

        page.click("#voucherNavRetryBtn")
        page.wait_for_selector("#voucherLinesContainer .voucher-line-row")

        assert len(calls) > calls_before_retry, "Try again did not re-issue the navigation request"
        assert page.locator("#voucherLinesContainer .voucher-nav-error").count() == 0
        assert skeleton_row_count(page) == 0
    finally:
        page.close()


def test_nav_buttons_disable_during_navigation_and_follow_position_after(browser, static_server):
    """Phase 2: no stacked navigations, and position — not the fetch — decides the final state."""
    page, mode, calls = open_verification_page(browser, static_server)
    try:
        seed_two_voucher_session(page)
        mode["value"] = "success"

        before = nav_button_state(page)
        assert before["prev"] is True, "Prev should be disabled on voucher 1 of 2 (position rule)"
        assert before["next"] is False, "Next should be live on voucher 1 of 2"

        during = start_nav_and_snapshot(page)
        assert during["prev"] is True
        assert during["next"] is True, "Next stayed clickable during the save phase — stacked navigations possible"
        assert "Saving" in during["badge"], "no processing indicator during the save phase"

        page.evaluate("() => window.__navPromise")
        page.wait_for_selector("#voucherLinesContainer .voucher-line-row")

        after = nav_button_state(page)
        # Now on voucher 2 of 2: the position rule must win over "the fetch finished".
        assert after["prev"] is False, "Prev stuck disabled after a completed navigation"
        assert after["next"] is True, "Next re-enabled on the last voucher — position rule lost to the fetch"
        assert after["badge"] is False, "processing badge left behind after the navigation finished"
    finally:
        page.close()


def test_second_navigation_click_is_dropped_while_one_is_running(browser, static_server):
    """The in-flight guard: a second call during a navigation must not start another one."""
    page, mode, calls = open_verification_page(browser, static_server)
    try:
        seed_two_voucher_session(page)
        mode["value"] = "success"

        page.evaluate(
            """() => {
                window.__navPromise = Promise.all([goToNextTransaction(), goToNextTransaction()]);
            }"""
        )
        page.evaluate("() => window.__navPromise")
        page.wait_for_selector("#voucherLinesContainer .voucher-line-row")

        assert len(calls) == 1, "a second overlapping navigation issued its own voucher_nav request"
    finally:
        page.close()


@pytest.mark.parametrize("outcome", ["boundary", "http_error"])
def test_nav_buttons_are_re_enabled_when_navigation_does_not_succeed(browser, static_server, outcome):
    """Stuck-disabled regression guard: a boundary re-renders only the lines and an error replaces
    only the table — neither touches the header, so nothing else would restore these buttons."""
    page, mode, calls = open_verification_page(browser, static_server)
    try:
        seed_two_voucher_session(page)
        mode["value"] = outcome

        page.evaluate("() => goToNextTransaction()")

        state = nav_button_state(page)
        # Still on voucher 1 of 2, so position says Prev off / Next on.
        assert state["next"] is False, "Next left stuck disabled after an unsuccessful navigation"
        assert state["prev"] is True, "Prev wrongly enabled — position says voucher 1 of 2"
        assert state["badge"] is False, "processing badge left behind"
    finally:
        page.close()


def test_boundary_restores_the_current_voucher_instead_of_an_error(browser, static_server):
    """A boundary is the server correctly saying "no voucher that way" — not a failure. The
    skeleton still has to go, but the auditor keeps the voucher they were already on."""
    page, mode, calls = open_verification_page(browser, static_server)
    try:
        seed_rendered_voucher(page)
        mode["value"] = "boundary"

        moved = page.evaluate("() => moveToAdjacentVoucher('next')")
        assert moved is False

        assert skeleton_row_count(page) == 0, "loading skeleton was left behind after a boundary response"
        assert page.locator("#voucherLinesContainer .voucher-nav-error").count() == 0
        assert page.locator("#voucherLinesContainer .voucher-line-row").count() == 1
        assert "last voucher" in page.inner_text("#verificationStatus").lower()
    finally:
        page.close()
