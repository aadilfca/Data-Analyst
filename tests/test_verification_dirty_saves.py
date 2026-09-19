from pathlib import Path

import pytest
from datetime import date
from playwright.sync_api import sync_playwright

from routers.verification import _evaluate_physical_bill_completion


VERIFICATION_JS = Path(__file__).resolve().parents[1] / "static" / "verification.js"
PROCEDURE_MODAL_JS = Path(__file__).resolve().parents[1] / "static" / "procedure_modal.js"
STYLE_CSS = Path(__file__).resolve().parents[1] / "static" / "style.css"


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
        page.add_style_tag(path=str(STYLE_CSS))
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


def test_procedure_badge_open_starts_on_voucher_line_and_explicit_document_target_wins(procedure_modal_page):
    result = procedure_modal_page.evaluate("""async () => {
        const capturedTabs = [];
        hostAdapter = {
            procedureModalState: {1: {tabs: {}}},
            voucherLineState: {1: {transactionType: 'Purchases'}},
            currentVoucherLines: [{detail_id: 1, row_data: {Account: 'Office supplies'}}],
            currentVoucherId: 10,
            fetchAuditGuidanceData: async () => ({
                documents: [{id: 7, name: 'Invoice'}],
                procedure_evidence: {checks: []}
            }),
            computeLineRiskData: () => ({risk: null}),
            getVoucherLineAlias: () => 'Office supplies',
            getVoucherLineAmount: () => '100',
            parseVoucherAmount: value => Number(value),
            VOUCHER_LINE_ACCOUNT_NAME_ALIASES: ['Account'],
            markLineDirty: () => {},
            markAllLinesDirty: () => {}
        };
        loadProcedureModalData = async () => ({});
        renderVoucherChecksBar = () => {};
        renderProcedureModalTabs = () => {};
        updateProcedureModalSavedNote = () => {};
        hideValidatePanel = () => {};
        activateProcedureTab = (uid, docId) => capturedTabs.push({uid, docId});
        const procedureBadge = document.createElement('button');
        await openProcedureModal(1, null, hostAdapter, procedureBadge);
        const badgeOpenTab = capturedTabs.at(-1).docId;
        await openProcedureModal(1, 7, hostAdapter, procedureBadge);
        const explicitDocumentTab = capturedTabs.at(-1).docId;
        return {badgeOpenTab, explicitDocumentTab};
    }""")

    assert result == {
        "badgeOpenTab": "__voucher_line__",
        "explicitDocumentTab": 7,
    }


def test_procedure_modal_initializes_missing_line_state_before_restoring_checkbox(procedure_modal_page):
    result = procedure_modal_page.evaluate("""async () => {
        const capturedTabs = [];
        hostAdapter = {
            procedureModalState: {},
            voucherLineState: {1: {transactionType: 'Purchases'}},
            currentVoucherLines: [{detail_id: 1, row_data: {Account: 'Office supplies'}}],
            currentVoucherId: 10,
            fetchAuditGuidanceData: async () => ({
                documents: [{id: 7, name: 'Invoice'}],
                procedure_evidence: {checks: []}
            }),
            computeLineRiskData: () => ({risk: null}),
            getVoucherLineAlias: () => 'Office supplies',
            getVoucherLineAmount: () => '100',
            parseVoucherAmount: value => Number(value),
            VOUCHER_LINE_ACCOUNT_NAME_ALIASES: ['Account'],
            markLineDirty: () => {},
            markAllLinesDirty: () => {}
        };
        loadProcedureModalData = async () => ({});
        renderVoucherChecksBar = () => {};
        renderProcedureModalTabs = () => {};
        updateProcedureModalSavedNote = () => {};
        hideValidatePanel = () => {};
        activateProcedureTab = (uid, docId) => capturedTabs.push({uid, docId});
        await openProcedureModal(1, null, hostAdapter, document.createElement('button'));
        const state = hostAdapter.procedureModalState[1];
        return {
            openedTab: capturedTabs.at(-1).docId,
            stateCreated: !!state,
            correct: state.voucherLineCorrect,
            dirty: state.voucherLineCorrectDirty
        };
    }""")

    assert result == {
        "openedTab": "__voucher_line__",
        "stateCreated": True,
        "correct": False,
        "dirty": False,
    }


def test_procedure_rail_uses_grey_green_red_states_and_account_head_query(procedure_modal_page):
    result = procedure_modal_page.evaluate("""() => {
        hostAdapter.escapeHtml = value => String(value ?? '');
        hostAdapter.currentVoucherId = 10;
        voucherChecksCache[10] = [];
        document.body.innerHTML = '<div id="procedureModalTabs"></div>';
        const doc = {
            id: 7, name: 'Invoice', template_id: 70,
            auth_roles: ['checked_by'], procedure_rules_configured: false,
            verification_fields: [{column_name: 'Amount', data_type: 'Numeric'}]
        };
        const voucherTab = {id: '__voucher_line__', name: 'Voucher Line', __voucherTab: true};
        const line = {
            documents: [voucherTab, doc], activeDocId: 7, assignedDocIds: new Set(),
            voucherLineAmount: 100, crossCheckResults: [], tabs: {}
        };
        hostAdapter.procedureModalState = {1: line};
        const renderStates = () => {
            renderProcedureModalTabs(1);
            return [...document.querySelectorAll('.procedure-tab-dot')].map(dot => dot.className);
        };
        const untouched = renderStates();
        line.assignedDocIds = new Set(['7']);
        line.tabs['7'] = {
            loaded: true, checklist: {checked_by: {is_checked: false}},
            selections: [{extract_amount: 50, row_data: {Amount: 50}}], observation: '',
            observationDirty: false, physicalBills: [], physicalBillSaved: false,
            physicalBillDirty: false, formEntries: [], formSaved: false, formDirty: false
        };
        const incomplete = renderStates();
        line.tabs['7'].checklist.checked_by.is_checked = true;
        line.tabs['7'].selections[0].extract_amount = 100;
        line.tabs['7'].selections[0].row_data.Amount = 100;
        const complete = renderStates();
        line.tabs['7'].selections[0].extract_amount = 50;
        line.tabs['7'].selections[0].row_data.Amount = 50;
        const mismatch = renderStates();
        const accountHead = getAccountHeadState(1);
        accountHead.matched = true;
        accountHead.suggestions = [{account_name: 'Office Supplies'}];
        accountHead.recommendationDirty = true;
        const voucherRecommendation = renderStates();
        const saveState = getVoucherTickSaveState();
        return {untouched, incomplete, complete, mismatch, voucherRecommendation,
            accountHeadNote: saveState.account_head_notes[0].observation_text};
    }""")

    assert result["untouched"] == ["procedure-tab-dot", "procedure-tab-dot"]
    assert result["incomplete"] == ["procedure-tab-dot", "procedure-tab-dot query"]
    assert result["complete"] == ["procedure-tab-dot", "procedure-tab-dot complete"]
    assert result["mismatch"] == ["procedure-tab-dot", "procedure-tab-dot query"]
    assert result["voucherRecommendation"] == ["procedure-tab-dot query", "procedure-tab-dot query"]
    assert result["accountHeadNote"] == "__account_head_recommendation__: Office Supplies"


def test_voucher_line_correct_checkbox_turns_dot_green_and_enters_save_payload(procedure_modal_page):
    result = procedure_modal_page.evaluate("""() => {
        hostAdapter.escapeHtml = value => String(value ?? '');
        hostAdapter.getClientId = () => null;
        hostAdapter.markLineDirty = () => {};
        hostAdapter.currentVoucherId = 10;
        hostAdapter.currentVoucherLines = [{detail_id: 1, row_data: {Account: 'Office supplies'}}];
        hostAdapter.VOUCHER_LINE_ACCOUNT_NAME_ALIASES = ['Account'];
        hostAdapter.getVoucherLineAlias = row => row.Account || '—';
        hostAdapter.procedureModalState = {
            1: {
                documents: [{id: '__voucher_line__', name: 'Voucher Line', __voucherTab: true}],
                activeDocId: '__voucher_line__', assignedDocIds: new Set(),
                voucherLineAmount: 100, crossCheckResults: [], tabs: {}
            }
        };
        document.body.innerHTML = '<div id="procedureModalPanels"></div><div id="procedureModalTabs"></div><span id="procedureModalSavedNote"></span>';
        renderVoucherApprovalPanel(1);
        renderProcedureModalTabs(1);
        const before = {
            checked: document.getElementById('procedureVoucherLineCorrect').checked,
            dot: document.querySelector('.procedure-tab-dot').className
        };
        const checkbox = document.getElementById('procedureVoucherLineCorrect');
        checkbox.checked = true;
        checkbox.dispatchEvent(new Event('change', {bubbles: true}));
        const after = {
            checked: checkbox.checked,
            dot: document.querySelector('.procedure-tab-dot').className,
            dirty: hostAdapter.procedureModalState[1].voucherLineCorrectDirty,
            payload: getVoucherTickSaveState().voucher_line_correctness
        };
        return {before, after};
    }""")

    assert result["before"] == {"checked": False, "dot": "procedure-tab-dot"}
    assert result["after"] == {
        "checked": True,
        "dot": "procedure-tab-dot complete",
        "dirty": True,
        "payload": [{"voucher_line_id": 1, "is_correct": True}],
    }


def test_voucher_line_correctness_flush_uses_server_system_document_id(procedure_modal_page):
    result = procedure_modal_page.evaluate("""async () => {
        window.ApiUtils = {
            apiFetchMutate: async (url, options) => {
                window.correctnessFlushBody = JSON.parse(options.body);
                return new Response(JSON.stringify({}), {
                    status: 200,
                    headers: {'Content-Type': 'application/json'}
                });
            }
        };
        procedureSystemDocumentIds.voucher_line_correctness = 502;
        hostAdapter.procedureModalState[1] = {
            voucherLineCorrect: true,
            voucherLineCorrectDirty: true,
            tabs: {}
        };
        await flushVoucherLineCorrectness(1);
        return window.correctnessFlushBody;
    }""")

    assert result["document_id"] == 502
    assert result["observation_text"] == "correct"


def test_account_head_recommendation_disables_correct_checkbox_until_cleared(procedure_modal_page):
    result = procedure_modal_page.evaluate("""() => {
        hostAdapter.escapeHtml = value => String(value ?? '');
        hostAdapter.getClientId = () => null;
        hostAdapter.markLineDirty = () => {};
        hostAdapter.currentVoucherId = 10;
        hostAdapter.currentVoucherLines = [{detail_id: 1, row_data: {Account: 'Office supplies'}}];
        hostAdapter.VOUCHER_LINE_ACCOUNT_NAME_ALIASES = ['Account'];
        hostAdapter.getVoucherLineAlias = row => row.Account || '—';
        hostAdapter.procedureModalState = {
            1: {
                documents: [{id: '__voucher_line__', name: 'Voucher Line', __voucherTab: true}],
                activeDocId: '__voucher_line__', assignedDocIds: new Set(),
                voucherLineAmount: 100, crossCheckResults: [], tabs: {},
                voucherLineCorrect: true, voucherLineCorrectTouched: true,
                voucherLineCorrectDirty: false
            }
        };
        document.body.innerHTML = '<div id="procedureModalPanels"></div><div id="procedureModalTabs"></div><span id="procedureModalSavedNote"></span>';
        renderVoucherApprovalPanel(1);
        const accountHead = getAccountHeadState(1);
        accountHead.selectedAccountName = 'Office Supplies';
        accountHead.matched = true;
        renderProcedureModalTabs(1);
        const checkbox = document.getElementById('procedureVoucherLineCorrect');
        const recommended = {
            checked: checkbox.checked,
            disabled: checkbox.disabled,
            dot: document.querySelector('.procedure-tab-dot').className,
            stateCorrect: hostAdapter.procedureModalState[1].voucherLineCorrect
        };
        accountHead.selectedAccountName = '';
        accountHead.matched = null;
        accountHead.suggestions = [];
        renderProcedureModalTabs(1);
        checkbox.checked = true;
        checkbox.dispatchEvent(new Event('change', {bubbles: true}));
        return {
            recommended,
            cleared: {
                checked: checkbox.checked,
                disabled: checkbox.disabled,
                dot: document.querySelector('.procedure-tab-dot').className
            }
        };
    }""")

    assert result == {
        "recommended": {
            "checked": False,
            "disabled": True,
            "dot": "procedure-tab-dot query",
            "stateCorrect": False,
        },
        "cleared": {
            "checked": True,
            "disabled": False,
            "dot": "procedure-tab-dot complete",
        },
    }


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


def test_procedure_browse_search_and_multi_selection(procedure_modal_page):
    result = procedure_modal_page.evaluate("""async () => {
        window.browseFetches = [];
        window.assignCalls = [];
        window.ApiUtils = {
            apiFetch: async url => {
                window.browseFetches.push(url);
                return new Response(JSON.stringify({
                    rows: [
                        {id: 11, row_data: {Reference: 'INV-11', Description: 'Office supplies'}, extract_date: '2026-09-01', extract_amount: 10, is_assigned: false},
                        {id: 12, row_data: {Reference: 'INV-12', Description: 'Office travel'}, extract_date: '2026-09-02', extract_amount: 20, is_assigned: false},
                        {id: 13, row_data: {Reference: 'INV-13', Description: 'Already linked'}, extract_date: '2026-09-03', extract_amount: 30, is_assigned: true, assigned_to_voucher_id: 'V-1', assigned_to_document_id: 2}
                    ],
                    total_rows: 3
                }), {status: 200, headers: {'Content-Type': 'application/json'}});
            },
            apiFetchMutate: async (url, options) => {
                const body = JSON.parse(options.body);
                window.assignCalls.push(body);
                return new Response(JSON.stringify({id: 100 + window.assignCalls.length}), {
                    status: 200, headers: {'Content-Type': 'application/json'}
                });
            }
        };
        hostAdapter = {
            procedureModalState: {1: {
                activeDocId: 2,
                documents: [{id: 2, name: 'Invoice', template_id: 20, verification_fields: [{column_name: 'Reference'}]}],
                assignedDocIds: new Set(),
                tabs: {2: {selections: []}}
            }},
            currentVoucherLines: [{detail_id: 1, client_data_id: 100}],
            escapeHtml: value => String(value ?? ''),
            markLineDirty: () => {},
            showStatus: (message, kind) => { window.lastBrowseStatus = {message, kind}; },
            getClientId: () => 1
        };
        renderProcedureModalTabs = () => {};
        renderProcedureModalPanel = () => {};
        invalidateProcedureModalData = () => {};
        procedureBrowseState = {
            uid: 1, docId: 2, page: 1, totalRows: 0, rows: [], statusFilter: 'all', search: '',
            selectedRows: new Map(), requestToken: 0
        };
        const searchInput = document.getElementById('procedureBrowseSearchInput');
        searchInput.value = 'INV';
        searchInput.dispatchEvent(new Event('input', {bubbles: true}));
        await new Promise(resolve => setTimeout(resolve, 350));
        const searchParams = new URLSearchParams(browseFetches[0].split('?')[1]);
        const selectAll = document.getElementById('procedureBrowseSelectAllCheckbox');
        selectAll.click();
        const linkButton = document.getElementById('procedureBrowseLinkSelectedBtn');
        const selectionBefore = linkButton.textContent.trim();
        const assignedCheckbox = document.querySelector('.procedure-browse-row-assigned .procedure-browse-row-checkbox');
        linkButton.click();
        await new Promise(resolve => {
            const startedAt = Date.now();
            const waitForAssignments = () => {
                if ((window.assignCalls.length === 2
                    && hostAdapter.procedureModalState[1].tabs[2].selections.length === 2
                    && window.lastBrowseStatus)
                    || Date.now() - startedAt > 1000) resolve();
                else setTimeout(waitForAssignments, 0);
            };
            waitForAssignments();
        });
        return {
            search: searchParams.get('search'),
            selectionBefore,
            assignedCheckboxLocked: !!assignedCheckbox?.disabled && !!assignedCheckbox?.checked,
            calls: window.assignCalls,
            selections: hostAdapter.procedureModalState[1].tabs[2].selections.map(selection => selection.client_data_id),
            status: window.lastBrowseStatus
        };
    }""")

    assert result["search"] == "INV"
    assert result["selectionBefore"] == "Link selected (2)"
    assert result["assignedCheckboxLocked"] is True
    assert [call["client_data_id"] for call in result["calls"]] == [11, 12]
    assert result["selections"] == [11, 12]
    assert result["status"] == {"message": "Evidence linked for 2 lines.", "kind": "success"}


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
            currentVoucherLines: [{detail_id: 1, client_data_id: 13}],
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
            {id: 13, row_data: {Reference: 'INV-13', Quantity: 120}, extract_date: '2026-09-03', extract_amount: 30, is_assigned: true, assigned_to_line_client_data_id: 13, assigned_to_document_id: 2}
        ];
        renderProcedureSearchDropdown(1, 2);
        const checkboxes = document.querySelectorAll('.procedure-result-checkbox:not(.procedure-result-select-all-checkbox)');
        const availableCheckboxes = Array.from(checkboxes).filter(checkbox => !checkbox.disabled);
        const assignedCheckbox = Array.from(checkboxes).find(checkbox => checkbox.disabled);
        const selectAllCheckbox = document.querySelector('.procedure-result-select-all-checkbox');
        const selectAllRow = selectAllCheckbox.closest('.procedure-result-item');
        const firstResultRow = document.querySelector('.procedure-result-item:not(.procedure-result-select-all-row)');
        const assignedRow = assignedCheckbox.closest('.procedure-result-item');
        const assignedMain = assignedRow?.querySelector('.procedure-result-main');
        const availableMain = availableCheckboxes[0]?.closest('.procedure-result-item')?.querySelector('.procedure-result-main');
        const selectAllMain = selectAllRow?.querySelector('.procedure-result-main');
        const layoutBoxes = {
            selectAllCheckbox: selectAllCheckbox.getBoundingClientRect().toJSON(),
            availableCheckbox: availableCheckboxes[0].getBoundingClientRect().toJSON(),
            assignedCheckbox: assignedCheckbox.getBoundingClientRect().toJSON(),
            availableLabel: availableMain.querySelector('.procedure-result-label').getBoundingClientRect().toJSON(),
            availableMeta: availableMain.querySelector('.procedure-result-meta').getBoundingClientRect().toJSON(),
            assignedLabel: assignedMain.querySelector('.procedure-result-label').getBoundingClientRect().toJSON(),
            assignedMeta: assignedMain.querySelector('.procedure-result-meta').getBoundingClientRect().toJSON(),
            assignedBadge: assignedMain.querySelector('.procedure-result-locked').getBoundingClientRect().toJSON()
        };
        const near = (left, right) => Math.abs(left - right) <= 1;
        const resultRowsAreGeometricallyAligned = near(layoutBoxes.selectAllCheckbox.x, layoutBoxes.availableCheckbox.x)
            && near(layoutBoxes.availableCheckbox.x, layoutBoxes.assignedCheckbox.x)
            && near(layoutBoxes.availableLabel.x, layoutBoxes.assignedLabel.x)
            && near(layoutBoxes.availableMeta.x, layoutBoxes.assignedMeta.x)
            && near(layoutBoxes.availableMeta.y - layoutBoxes.availableLabel.y, layoutBoxes.assignedMeta.y - layoutBoxes.assignedLabel.y)
            && near(layoutBoxes.assignedCheckbox.y + layoutBoxes.assignedCheckbox.height / 2,
                layoutBoxes.assignedLabel.y + (layoutBoxes.assignedMeta.y + layoutBoxes.assignedMeta.height - layoutBoxes.assignedLabel.y) / 2)
            && near(layoutBoxes.assignedBadge.y + layoutBoxes.assignedBadge.height / 2,
                layoutBoxes.assignedLabel.y + layoutBoxes.assignedLabel.height / 2);
        assignedRow?.querySelector('.procedure-result-label')?.click();
        const assignedTextDidNotLink = window.assignCalls.length === 0;
        const selectAllPlacementIsSymmetric = !!selectAllRow
            && !!firstResultRow
            && selectAllRow.nextElementSibling === firstResultRow
            && selectAllRow.classList.contains('procedure-result-select-all-row')
            && selectAllCheckbox.classList.contains('procedure-result-checkbox');
        const contentColumnsAreSymmetric = !!assignedMain?.classList.contains('procedure-result-content')
            && !!availableMain?.classList.contains('procedure-result-content')
            && !!selectAllMain?.classList.contains('procedure-result-content')
            && assignedRow?.querySelector('.procedure-result-locked')?.textContent === 'Already linked here';
        const assignedCheckboxUsesResultControl = assignedCheckbox?.classList.contains('procedure-result-checkbox')
            && assignedCheckbox?.closest('.procedure-result-item')?.classList.contains('locked');
        selectAllCheckbox.click();
        const selectAllWorked = availableCheckboxes.every(checkbox => checkbox.checked)
            && assignedCheckbox?.checked === true
            && assignedCheckbox?.closest('.procedure-result-item')?.classList.contains('locked');
        selectAllCheckbox.click();
        const clearAllWorked = availableCheckboxes.every(checkbox => !checkbox.checked)
            && assignedCheckbox?.checked === true;
        const firstMeta = document.querySelector('.procedure-result-meta').textContent.trim();
        const firstContent = document.querySelector('.procedure-result-item:not(.procedure-result-select-all-row) .procedure-result-content');
        firstContent.click();
        const checkedAfterBlankContentClick = checkboxes[0].checked;
        const masterShowsPartialSelection = selectAllCheckbox.indeterminate;
        firstContent.click();
        const uncheckedAfterBlankContentClick = checkboxes[0].checked;
        const firstRow = document.querySelector('.procedure-result-item:not(.procedure-result-select-all-row)');
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
            availableCheckboxCount: availableCheckboxes.length,
            assignedCheckboxIsLockedAndChecked: !!assignedCheckbox?.checked
                && !!assignedCheckbox?.disabled
                && !!assignedCheckbox?.closest('.procedure-result-item')?.classList.contains('locked'),
            selectAllPlacementIsSymmetric,
            contentColumnsAreSymmetric: !!contentColumnsAreSymmetric,
            resultRowsAreGeometricallyAligned,
            assignedTextDidNotLink,
            assignedCheckboxUsesResultControl: !!assignedCheckboxUsesResultControl,
            selectAllWorked,
            clearAllWorked,
            masterShowsPartialSelection,
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

    assert result["checkboxCount"] == 3
    assert result["availableCheckboxCount"] == 2
    assert result["assignedCheckboxIsLockedAndChecked"] is True
    assert result["selectAllPlacementIsSymmetric"] is True
    assert result["contentColumnsAreSymmetric"] is True
    assert result["resultRowsAreGeometricallyAligned"] is True
    assert result["assignedTextDidNotLink"] is True
    assert result["assignedCheckboxUsesResultControl"] is True
    assert result["selectAllWorked"] is True
    assert result["clearAllWorked"] is True
    assert result["masterShowsPartialSelection"] is True
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
        document.querySelector('.procedure-result-item:not(.procedure-result-select-all-row) .procedure-result-label').click();
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
        const before = {
            visible: !clearButton.hidden,
            rows: document.querySelectorAll('.procedure-result-item:not(.procedure-result-select-all-row)').length
        };
        clearButton.click();
        return {
            before,
            after: {
                visible: !clearButton.hidden,
                value: document.getElementById('procedureSearchInput').value,
                    rows: document.querySelectorAll('.procedure-result-item:not(.procedure-result-select-all-row)').length,
                searchTerm: tabState.searchTerm,
                pending: tabState.pendingSelectionIds
            }
        };
    }""")

    assert result == {
        "before": {"visible": True, "rows": 1},
        "after": {"visible": False, "value": "", "rows": 0, "searchTerm": "", "pending": []}
    }
