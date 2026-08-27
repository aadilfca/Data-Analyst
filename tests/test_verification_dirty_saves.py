from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright


VERIFICATION_JS = Path(__file__).resolve().parents[1] / "static" / "verification.js"


@pytest.fixture()
def dirty_page():
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content("<html><body></body></html>")
        page.add_script_tag(content="""
            var API_BASE = '';
            async function flushProcedureObservations() {}
            async function flushAccountHeadNoMatchNote() {}
        """)
        page.add_script_tag(path=str(VERIFICATION_JS))
        page.evaluate("""
            window.fetchCalls = [];
            window.fetch = async (url, options) => {
                window.fetchCalls.push({url, options});
                return new Response(JSON.stringify({cleared_status: 'Cleared'}), {
                    status: 200,
                    headers: {'Content-Type': 'application/json'}
                });
            };
            seedVoucherLineState([
                {detail_id: 1, transaction_type: 'Purchases', line_items: []},
                {detail_id: 2, transaction_type: 'Purchases', line_items: []},
                {detail_id: 3, transaction_type: 'Purchases', line_items: []}
            ]);
        """)
        yield page
        browser.close()


def test_untouched_voucher_issues_no_save_requests(dirty_page):
    result = dirty_page.evaluate("saveAllVoucherLines()")

    assert result == {"savedCount": 0, "failedCount": 0, "skippedCount": 3}
    assert dirty_page.evaluate("window.fetchCalls.length") == 0


def test_one_changed_line_issues_one_save(dirty_page):
    result = dirty_page.evaluate("""async () => {
        markVoucherLineDirty(2, 'test edit');
        return await saveAllVoucherLines();
    }""")

    assert result == {"savedCount": 1, "failedCount": 0, "skippedCount": 2}
    assert dirty_page.evaluate("window.fetchCalls.length") == 1
    assert not dirty_page.evaluate("isVoucherLineDirty(2)")


def test_voucher_wide_change_marks_every_line(dirty_page):
    dirty = dirty_page.evaluate("""() => {
        markAllVoucherLinesDirty('voucher check changed');
        return [1, 2, 3].map(isVoucherLineDirty);
    }""")

    assert dirty == [True, True, True]


def test_failed_save_remains_dirty_for_retry(dirty_page):
    result = dirty_page.evaluate("""async () => {
        window.fetch = async () => new Response(JSON.stringify({detail: 'failed'}), {
            status: 500,
            headers: {'Content-Type': 'application/json'}
        });
        markVoucherLineDirty(1, 'test edit');
        const saveResult = await saveLineItems(1);
        return {saveResult, dirty: isVoucherLineDirty(1)};
    }""")

    assert not result["saveResult"]["ok"]
    assert result["dirty"]


def test_edit_during_save_preserves_newer_dirty_revision(dirty_page):
    remains_dirty = dirty_page.evaluate("""async () => {
        let releaseFetch;
        window.fetch = () => new Promise(resolve => { releaseFetch = resolve; });
        markVoucherLineDirty(1, 'first edit');
        const savePromise = saveLineItems(1);
        await new Promise(resolve => setTimeout(resolve, 0));
        markVoucherLineDirty(1, 'newer edit');
        releaseFetch(new Response(JSON.stringify({cleared_status: 'Cleared'}), {
            status: 200,
            headers: {'Content-Type': 'application/json'}
        }));
        await savePromise;
        return isVoucherLineDirty(1);
    }""")

    assert remains_dirty


def test_forced_verify_save_is_not_repeated_by_navigation(dirty_page):
    result = dirty_page.evaluate("""async () => {
        await saveLineItems(1, {force: true});
        const afterVerify = window.fetchCalls.length;
        const navResult = await saveAllVoucherLines();
        return {afterVerify, afterNavigation: window.fetchCalls.length, navResult};
    }""")

    assert result["afterVerify"] == 1
    assert result["afterNavigation"] == 1
    assert result["navResult"] == {"savedCount": 0, "failedCount": 0, "skippedCount": 3}
