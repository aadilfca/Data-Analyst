from pathlib import Path

import pytest
from datetime import date
from playwright.sync_api import sync_playwright

from routers.verification import _evaluate_physical_bill_completion


VERIFICATION_JS = Path(__file__).resolve().parents[1] / "static" / "verification.js"
PROCEDURE_MODAL_JS = Path(__file__).resolve().parents[1] / "static" / "procedure_modal.js"


def test_physical_bill_completion_accepts_postgres_date_value():
    bill = {
        "bill_no": "VB-1",
        "bill_date": date(2026, 8, 28),
        "vendor_name": "Vendor",
        "items": [100],
    }
    assert _evaluate_physical_bill_completion([bill], 100) is True


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


@pytest.fixture()
def procedure_modal_page():
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content("<html><body></body></html>")
        page.add_script_tag(content="var API_BASE = ''; ")
        page.add_script_tag(path=str(PROCEDURE_MODAL_JS))
        page.evaluate("""
            window.modalSaveCalls = 0;
            window.modalStatusMessages = [];
            hostAdapter = {
                procedureModalState: {1: {tabs: {}}},
                voucherLineState: {1: {transactionType: 'Purchases'}},
                currentVoucherLines: [],
                isLineDirty: () => false,
                saveAndVerifyVoucher: async () => {
                    window.modalSaveCalls += 1;
                    return {ok: true};
                },
                showStatus: (message, kind) => window.modalStatusMessages.push({message, kind}),
                updateProceduresCellForLine: () => Promise.resolve(),
                updateObservationsCellForLine: () => Promise.resolve()
            };
            currentProcedureModalUid = 1;
            procedureModal.style.display = 'flex';
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


def test_save_and_verify_uses_one_combined_request(dirty_page):
    result = dirty_page.evaluate("""async () => {
        currentSessionId = 42;
        currentVoucherLines = [{detail_id: 1, cleared_status: 'Query'}];
        voucherLineState[1].lineItems = [];
        voucherLineState[1].transactionType = 'Purchases';
        voucherLineState[1].procedureData = {};
        const saveResult = await saveAndVerifyVoucher(1);
        return {saveResult, calls: window.fetchCalls};
    }""")

    assert result["saveResult"]["ok"]
    assert len(result["calls"]) == 1
    assert result["calls"][0]["url"] == "/api/verification/save_and_verify_voucher_bundle"
    assert "save_line_items" not in result["calls"][0]["url"]
    assert "verify_voucher_checks" not in result["calls"][0]["url"]


def test_save_and_verify_failure_is_reported_without_extra_request(dirty_page):
    result = dirty_page.evaluate("""async () => {
        currentSessionId = 42;
        window.fetch = async (url, options) => {
            window.fetchCalls.push({url, options});
            return new Response(JSON.stringify({detail: 'verification failed'}), {
                status: 500,
                headers: {'Content-Type': 'application/json'}
            });
        };
        const saveResult = await saveAndVerifyVoucher(1);
        return {saveResult, calls: window.fetchCalls};
    }""")

    assert not result["saveResult"]["ok"]
    assert len(result["calls"]) == 1


def test_clean_procedure_modal_close_is_immediate_and_does_not_save(procedure_modal_page):
    result = procedure_modal_page.evaluate("""async () => {
        await closeProcedureModal();
        return {
            display: procedureModal.style.display,
            currentUid: currentProcedureModalUid,
            saveCalls: window.modalSaveCalls
        };
    }""")

    assert result == {"display": "none", "currentUid": None, "saveCalls": 0}


def test_dirty_procedure_modal_close_saves_before_hiding(procedure_modal_page):
    result = procedure_modal_page.evaluate("""async () => {
        hostAdapter.isLineDirty = () => true;
        await closeProcedureModal();
        return {
            display: procedureModal.style.display,
            currentUid: currentProcedureModalUid,
            saveCalls: window.modalSaveCalls
        };
    }""")

    assert result == {"display": "none", "currentUid": None, "saveCalls": 1}


def test_failed_dirty_procedure_modal_close_stays_open_and_reports_error(procedure_modal_page):
    result = procedure_modal_page.evaluate("""async () => {
        hostAdapter.isLineDirty = () => true;
        hostAdapter.saveAndVerifyVoucher = async () => ({
            ok: false,
            error: {detail: 'save failed'}
        });
        await closeProcedureModal();
        return {
            display: procedureModal.style.display,
            currentUid: currentProcedureModalUid,
            messages: window.modalStatusMessages
        };
    }""")

    assert result["display"] == "flex"
    assert result["currentUid"] == 1
    assert result["messages"][-1] == {"message": "save failed", "kind": "error"}


def test_rulebook_change_alert_shows_change_and_returns_user_choice(dirty_page):
    result = dirty_page.evaluate("""async () => {
        const recoverPromise = showRulebookChangeAlert({
            changes: [{entity_type: 'Voucher-level Check', entity_name: 'Test check', action: 'Deleted'}],
            sessionVersion: 4,
            currentVersion: 5
        });
        const alert = document.getElementById('rulebookChangeAlert');
        const visibleText = alert.textContent;
        alert.querySelector('[data-rulebook-alert-action="recover"]').click();
        const recovered = await recoverPromise;

        const cancelPromise = showRulebookChangeAlert({changes: [], sessionVersion: 5, currentVersion: 6});
        document.querySelector('[data-rulebook-alert-action="cancel"]').click();
        const cancelled = await cancelPromise;
        return {visibleText, recovered, cancelled, remaining: !!document.getElementById('rulebookChangeAlert')};
    }""")

    assert 'Voucher-level Check “Test check”' in result["visibleText"]
    assert 'Session version 4' in result["visibleText"]
    assert result["recovered"] is True
    assert result["cancelled"] is False
    assert result["remaining"] is False


def test_grouped_save_clears_only_revisions_included_in_request(dirty_page):
    result = dirty_page.evaluate("""async () => {
        markAllVoucherLinesDirty('voucher check changed');
        const saveResult = await saveAndVerifyVoucher(1);
        const afterSave = [1, 2, 3].map(isVoucherLineDirty);
        return {saveResult, afterSave, requests: window.fetchCalls.length};
    }""")

    assert result["saveResult"]["ok"]
    assert result["afterSave"] == [False, False, False]
    assert result["requests"] == 1


def test_grouped_save_preserves_edit_made_during_request(dirty_page):
    result = dirty_page.evaluate("""async () => {
        let releaseFetch;
        window.fetch = () => new Promise(resolve => { releaseFetch = resolve; });
        markAllVoucherLinesDirty('voucher check changed');
        const savePromise = saveAndVerifyVoucher(1);
        await new Promise(resolve => setTimeout(resolve, 0));
        markVoucherLineDirty(2, 'newer edit');
        releaseFetch(new Response(JSON.stringify({line_statuses: {}}), {
            status: 200,
            headers: {'Content-Type': 'application/json'}
        }));
        const saveResult = await savePromise;
        return {saveResult, dirty: [1, 2, 3].map(isVoucherLineDirty)};
    }""")

    assert result["saveResult"]["ok"]
    assert result["dirty"] == [False, True, False]


def test_procedure_search_dropdown_links_multiple_selected_rows(procedure_modal_page):
    result = procedure_modal_page.evaluate("""async () => {
        window.assignCalls = [];
        window.ApiUtils = {
            apiFetchMutate: async (url, options) => {
                window.assignCalls.push({url, body: JSON.parse(options.body)});
                return new Response(JSON.stringify({id: 100 + window.assignCalls.length}), {
                    status: 200,
                    headers: {'Content-Type': 'application/json'}
                });
            }
        };
        hostAdapter = {
            procedureModalState: {1: {
                tabs: {},
                assignedDocIds: new Set(),
                documents: []
            }},
            currentVoucherLines: [],
            escapeHtml: value => String(value),
            markLineDirty: () => {},
            showStatus: (message, kind) => { window.lastModalStatus = {message, kind}; }
        };
        renderProcedureModalTabs = () => {};
        renderProcedureModalPanel = () => {};
        invalidateProcedureModalData = () => {};
        document.body.insertAdjacentHTML('beforeend', `
            <div class="procedure-search-input-wrap">
                <input id="procedureSearchInput" value="invoice">
                <button id="procedureSearchClearBtn" hidden>×</button>
            </div>
            <div id="procedureSearchDropdown"></div>
        `);
        const tabState = getProcedureTabState(1, 2);
        tabState.searchTerm = 'invoice';
        tabState.docKeyColumn = 'Reference';
        tabState.searchResults = [
            {id: 11, row_data: {Reference: 'INV-11', Quantity: 1}, extract_date: '2026-09-01', extract_amount: 10, is_assigned: false},
            {id: 12, row_data: {Reference: 'INV-12', Quantity: 10}, extract_date: '2026-09-02', extract_amount: 20, is_assigned: false},
            {id: 13, row_data: {Reference: 'INV-13', Quantity: 120}, extract_date: '2026-09-03', extract_amount: 30, is_assigned: true}
        ];
        renderProcedureSearchDropdown(1, 2);
        const checkboxes = document.querySelectorAll('.procedure-result-checkbox');
        const firstMeta = document.querySelector('.procedure-result-meta').textContent.trim();
        const firstContent = document.querySelector('.procedure-result-content');
        firstContent.click();
        const checkedAfterBlankContentClick = checkboxes[0].checked;
        firstContent.click();
        const uncheckedAfterBlankContentClick = checkboxes[0].checked;
        const firstRow = document.querySelector('.procedure-result-item');
        firstRow.click();
        const checkedAfterFirstPaddingClick = checkboxes[0].checked;
        firstRow.click();
        const uncheckedAfterSecondPaddingClick = checkboxes[0].checked;
        firstRow.click();
        const checkedAfterThirdPaddingClick = checkboxes[0].checked;
        checkboxes[1].click();
        const linkButton = document.getElementById('procedureSearchLinkSelectedBtn');
        const selectedLabel = linkButton.textContent.trim();
        linkButton.click();
        await new Promise(resolve => {
            const startedAt = Date.now();
            const waitForAssignments = () => {
                if ((window.assignCalls.length === 2 && tabState.selections.length === 2)
                    || Date.now() - startedAt > 1000) resolve();
                else setTimeout(waitForAssignments, 0);
            };
            waitForAssignments();
        });
        return {
            checkboxCount: checkboxes.length,
            firstMeta,
            clearVisible: !document.getElementById('procedureSearchClearBtn').hidden,
            blankAreaToggleWorked: checkedAfterBlankContentClick && !uncheckedAfterBlankContentClick,
            paddingToggleWorked: checkedAfterFirstPaddingClick
                && !uncheckedAfterSecondPaddingClick
                && checkedAfterThirdPaddingClick,
            selectedLabel,
            calls: window.assignCalls,
            selections: tabState.selections.map(selection => selection.client_data_id),
            status: window.lastModalStatus
        };
    }""")

    assert result["checkboxCount"] == 2
    assert result["firstMeta"] == "2026-09-01 · Quantity: 1"
    assert result["clearVisible"] is True
    assert result["blankAreaToggleWorked"] is True
    assert result["paddingToggleWorked"] is True
    assert result["selectedLabel"] == "Link selected (2)"
    assert [call["body"]["client_data_id"] for call in result["calls"]] == [11, 12]
    assert result["selections"] == [11, 12]
    assert result["status"] == {"message": "Evidence linked for 2 lines.", "kind": "success"}


def test_procedure_search_text_click_links_and_closes_dropdown(procedure_modal_page):
    result = procedure_modal_page.evaluate("""async () => {
        window.assignCalls = [];
        window.ApiUtils = {
            apiFetchMutate: async (url, options) => {
                window.assignCalls.push({url, body: JSON.parse(options.body)});
                return new Response(JSON.stringify({id: 201}), {
                    status: 200,
                    headers: {'Content-Type': 'application/json'}
                });
            }
        };
        hostAdapter = {
            procedureModalState: {1: {tabs: {}, assignedDocIds: new Set(), documents: []}},
            currentVoucherLines: [],
            escapeHtml: value => String(value),
            markLineDirty: () => {},
            showStatus: (message, kind) => { window.lastModalStatus = {message, kind}; }
        };
        renderProcedureModalTabs = () => {};
        renderProcedureModalPanel = () => renderProcedureSearchDropdown(1, 2);
        invalidateProcedureModalData = () => {};
        document.body.insertAdjacentHTML('beforeend', `
            <div class="procedure-search-input-wrap">
                <input id="procedureSearchInput" value="invoice">
                <button id="procedureSearchClearBtn" hidden>×</button>
            </div>
            <div id="procedureSearchDropdown"></div>
        `);
        const tabState = getProcedureTabState(1, 2);
        tabState.searchTerm = 'invoice';
        tabState.docKeyColumn = 'Reference';
        tabState.searchResults = [
            {id: 11, row_data: {Reference: 'INV-11', Quantity: 1}, extract_date: '2026-09-01', is_assigned: false},
            {id: 12, row_data: {Reference: 'INV-12', Quantity: 10}, extract_date: '2026-09-02', is_assigned: false}
        ];
        renderProcedureSearchDropdown(1, 2);
        document.querySelector('.procedure-result-label').click();
        await new Promise(resolve => {
            const startedAt = Date.now();
            const waitForAssignment = () => {
                if ((window.assignCalls.length === 1 && tabState.selections.length === 1)
                    || Date.now() - startedAt > 1000) resolve();
                else setTimeout(waitForAssignment, 0);
            };
            waitForAssignment();
        });
        return {
            calls: window.assignCalls,
            selections: tabState.selections.map(selection => selection.client_data_id),
            rows: document.querySelectorAll('.procedure-result-item').length,
            status: window.lastModalStatus
        };
    }""")

    assert [call["body"]["client_data_id"] for call in result["calls"]] == [11]
    assert result["selections"] == [11]
    assert result["rows"] == 0
    assert result["status"] == {"message": "✅ Evidence linked.", "kind": "success"}


def test_procedure_search_clear_button_closes_dropdown(procedure_modal_page):
    result = procedure_modal_page.evaluate("""() => {
        hostAdapter = {
            procedureModalState: {1: {tabs: {}}},
            currentVoucherLines: [],
            escapeHtml: value => String(value)
        };
        document.body.insertAdjacentHTML('beforeend', `
            <div class="procedure-search-input-wrap">
                <input id="procedureSearchInput" value="invoice">
                <button id="procedureSearchClearBtn" hidden>×</button>
            </div>
            <div id="procedureSearchDropdown"></div>
        `);
        const tabState = getProcedureTabState(1, 2);
        tabState.searchTerm = 'invoice';
        tabState.docKeyColumn = 'Reference';
        tabState.pendingSelectionIds = ['11'];
        tabState.searchResults = [
            {id: 11, row_data: {Reference: 'INV-11', Quantity: 1}, extract_date: '2026-09-01', is_assigned: false}
        ];
        renderProcedureSearchDropdown(1, 2);
        document.getElementById('procedureSearchClearBtn').addEventListener('click', () => {
            closeProcedureSearchDropdown(1, 2);
        });
        const clearButton = document.getElementById('procedureSearchClearBtn');
        const before = {visible: !clearButton.hidden, rows: document.querySelectorAll('.procedure-result-item').length};
        clearButton.click();
        return {
            before,
            after: {
                visible: !clearButton.hidden,
                value: document.getElementById('procedureSearchInput').value,
                rows: document.querySelectorAll('.procedure-result-item').length,
                searchTerm: tabState.searchTerm,
                pending: tabState.pendingSelectionIds
            }
        };
    }""")

    assert result == {
        "before": {"visible": True, "rows": 1},
        "after": {"visible": False, "value": "", "rows": 0, "searchTerm": "", "pending": []}
    }
