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


def _preview_response(page_number, limit, search=None, account=None):
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
                "Document No": "{}INV-{}".format(account + '-' if account else '', row_number),
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


def open_databank(browser, static_server, *, ledger_accounts=None):
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
                "data_purpose": 'account_ledger' if ledger_accounts is not None else 'standard',
                "columns": [
                    {"column_name": "Document No", "data_type": "Text"},
                    {"column_name": "Date", "data_type": "Date"},
                    {"column_name": "Amount", "data_type": "Numeric"},
                ],
            }))
            return
        if path.endswith('/api/data/ledger-accounts'):
            calls.append({'kind':'accounts','page':0,'search':None,'params':query})
            route.fulfill(json={'accounts':ledger_accounts or []})
            return
        if path.endswith('/api/data/history'):
            history_page = int(query.get('page', ['1'])[0])
            calls.append({'kind':'history','page':history_page,'params':query})
            action = query.get('action', ['all'])[0]
            row_action = 'deleted' if action == 'deleted' or (action == 'all' and history_page == 2) else 'changed'
            route.fulfill(status=200, content_type='application/json', body=json.dumps({
                'client_id': 1,
                'template_id': 1,
                'template_name': 'Invoices',
                'data_purpose': 'account_ledger' if ledger_accounts is not None else 'standard',
                'document_key_column': 'Document No',
                'columns': [
                    {'column_name':'Document No','data_type':'Text'},
                    {'column_name':'Date','data_type':'Date'},
                    {'column_name':'Amount','data_type':'Numeric'},
                ],
                'page': history_page,
                'limit': 25,
                'total': 1 if action != 'all' else 26,
                'total_pages': 1 if action != 'all' else 2,
                'rows': [] if action == 'changed' and history_page > 1 else [{
                    'id': history_page,
                    'client_data_id': 100 + history_page,
                    'action': row_action,
                    'changed_at': '2026-11-04T10:30:00+00:00',
                    'changed_by': 'Analyst',
                    'row_data': {'Document No': '<old-invoice>', 'Date':'2026-01-01', 'Amount': 100},
                    'changed_fields': {'Amount': {'old_value':100, 'new_value':125}} if row_action == 'changed' else None,
                }],
            }))
            return
        if path.endswith('/api/data/ledger-accounts'):
            calls.append({'kind':'history_accounts','params':query})
            route.fulfill(json={'accounts':ledger_accounts or []})
            return
        if path.endswith("/api/data/preview"):
            page_number = int(query.get("page", ["1"])[0])
            limit = int(query.get("limit", ["50"])[0])
            search = query.get("search", [None])[0]
            account = query.get('ledger_account_code', [None])[0]
            calls.append({"kind":"preview", "page": page_number, "search": search, "account":account,"params":query})
            route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps(_preview_response(page_number, limit, search, account)),
            )
            return

        route.fulfill(status=200, content_type="application/json", body=json.dumps({}))

    page.route("**/api/**", handle_api)
    page.goto("{}/static/databank.html".format(static_server), wait_until="load")
    page.wait_for_selector("#yourDatasetsBody tr")
    return page, calls


def preview_calls(calls, page_number, search=None):
    return [call for call in calls if call["page"] == page_number and call["search"] == search]


def test_changed_deleted_history_view_is_scoped_and_paginated(browser, static_server):
    page, calls = open_databank(browser, static_server)
    try:
        page.locator('button[title="Changed/Deleted data"]').click()
        page.wait_for_selector('#historyModal', state='visible')
        page.wait_for_function("() => document.querySelector('#historyTableBody').innerText.includes('<old-invoice>')")

        assert page.locator('#historyTitle').inner_text() == '📜 Changed/Deleted data — Invoices'
        assert page.locator('#historyTableBody').inner_text().find('Changed') >= 0
        assert page.locator('#historyTableBody').inner_text().find('Analyst') >= 0
        assert page.locator('#historyTableBody').inner_text().find('old-invoice') >= 0
        assert page.locator('#historyTableBody').inner_text().find('100') >= 0
        assert page.locator('#historyTableBody').inner_text().find('125') >= 0
        assert page.locator('#historyTableBody').inner_text().find('2026-01-01') == -1
        history_calls = [call for call in calls if call['kind'] == 'history']
        assert history_calls[0]['params']['client_id'] == ['1']
        assert history_calls[0]['params']['template_id'] == ['1']
        assert history_calls[0]['params']['page'] == ['1']

        page.locator('#historyActionFilter').select_option('changed')
        page.wait_for_function("() => document.querySelector('#historyTableBody').innerText.includes('Changed')")
        assert [call['params']['action'][0] for call in calls if call['kind'] == 'history'][-1] == 'changed'
        page.locator('#historyActionFilter').select_option('deleted')
        page.wait_for_function("() => document.querySelector('#historyTableBody').innerText.includes('Deleted')")
        assert [call['params']['action'][0] for call in calls if call['kind'] == 'history'][-1] == 'deleted'
        page.locator('#historyActionFilter').select_option('all')
        page.wait_for_function("() => document.querySelector('#historyTableBody').innerText.includes('Changed')")
        page.locator('#historyNextBtn').click()
        page.wait_for_function("() => document.querySelector('#historyTableBody').innerText.includes('Deleted')")
        assert page.locator('#historyTableBody').inner_text().find('2026-01-01') >= 0
        assert [call['page'] for call in calls if call['kind'] == 'history'] == [1, 1, 1, 1, 2]
        page.locator('#closeHistoryBtn').click()
        assert page.locator('#historyModal').is_hidden()
    finally:
        page.close()


def test_history_action_remains_available_when_template_has_no_active_rows(browser, static_server):
    page, _calls = open_databank(browser, static_server)
    try:
        actions = page.evaluate("""() => {
            const cell = buildDatasetActionButtons({template_id: 88, template_name: 'Empty ledger', total_row_count: 0});
            return Array.from(cell.querySelectorAll('button')).map(button => button.title);
        }""")
        assert actions == ['Changed/Deleted data']
    finally:
        page.close()


def test_ledger_history_selector_filters_by_account(browser, static_server):
    page, calls = open_databank(browser, static_server, ledger_accounts=ACCOUNTS)
    try:
        page.locator('button[title="Changed/Deleted data"]').click()
        page.wait_for_selector('#historyModal', state='visible')
        page.wait_for_function("() => !document.querySelector('#historyLedgerControl').hidden")
        page.locator('#historyLedgerSelect').select_option('A-1')
        page.wait_for_function("() => performance.getEntriesByType('resource').some(entry => entry.name.includes('ledger_account_code=A-1'))")
        history_calls = [call for call in calls if call['kind'] == 'history']
        assert history_calls[-1]['params']['ledger_account_code'] == ['A-1']
    finally:
        page.close()


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
        assert page.locator('#previewLedgerField').is_hidden()
    finally:
        page.close()


ACCOUNTS = [{'account_code':'A-1','account_name':'Cash <Alpha>'},
            {'account_code':'B-2','account_name':'Bank beta'}]


def open_ledger(page):
    page.evaluate("() => { pageSizeSelect.value='25'; return openPreview(1,'Universal book'); }")
    page.wait_for_function("previewAccountState==='ledger' && !previewLedgerLoading")


def select_account(page, text, *, keyboard=False):
    page.locator('#previewLedgerInput').fill(text)
    if keyboard:
        page.locator('#previewLedgerInput').press('ArrowDown')
        assert page.locator('#previewLedgerInput').get_attribute('aria-activedescendant')
        page.locator('#previewLedgerInput').press('Enter')
    else:
        page.locator('#previewLedgerOptions [role=option]').first.click()


def test_ledger_preview_retains_server_sequence_and_text_balances_across_pages(browser,static_server):
    page,_=open_databank(browser,static_server,ledger_accounts=ACCOUNTS[:1])
    def respond(route):
        query=parse_qs(urlparse(route.request.url).query)
        number=int(query.get('page',['1'])[0])
        payload=_preview_response(number,25,account='A-1')
        # Same-day voucher numbers deliberately descend while balances remain attached.
        values=[(99,'V9','2026-01-01','90 DR'),(11,'V1','2026-01-01','80 DR')] if number==1 else [(4,'V8','2026-01-02','110 DR')]
        payload['rows']=[dict(payload['rows'][0],id=key,document_group_id=voucher,
            row_data={'Document No':voucher,'Date':day,'Amount':10,'Balance':balance}) for key,voucher,day,balance in values]
        route.fulfill(json=payload)
    page.route('**/api/templates/1',lambda route:route.fulfill(json={'id':1,'data_purpose':'account_ledger',
        'has_subtotals_by_default':False,'columns':[{'column_name':n,'data_type':t} for n,t in
        [('Document No','Text'),('Date','Date'),('Amount','Numeric'),('Balance','Text')]]}))
    page.route('**/api/data/preview?*',respond)
    try:
        open_ledger(page)
        page.wait_for_function("previewTableBody.innerText.includes('V9')")
        assert page.locator('#previewTableBody tr td:nth-child(2)').all_text_contents()==['V9','V1']
        assert page.locator('#previewTableBody tr td:nth-child(5)').all_text_contents()==['90 DR','80 DR']
        page.locator('#nextPageBtn').click()
        page.wait_for_function("previewTableBody.innerText.includes('V8')")
        assert page.locator('#previewTableBody tr td:nth-child(5)').all_text_contents()==['110 DR']
        page.locator('#prevPageBtn').click()
        page.wait_for_function("previewTableBody.innerText.includes('V9')")
        assert page.locator('#previewTableBody tr td:nth-child(2)').all_text_contents()==['V9','V1']
    finally:page.close()


def test_ledger_search_and_dropdown_scope_pages_and_cache_by_account(browser, static_server):
    page, calls = open_databank(browser, static_server, ledger_accounts=ACCOUNTS)
    errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
    try:
        open_ledger(page)
        assert page.locator('#previewLedgerField').is_visible()
        assert not [c for c in calls if c['kind']=='preview']
        assert 'Select an account' in page.locator('#previewTableBody').inner_text()
        select_account(page,'beta',keyboard=True)
        page.wait_for_function("previewTableBody.innerText.includes('B-2-INV-1')")
        page.wait_for_function("previewPageCache.size>=2")
        assert all(c['account']=='B-2' for c in calls if c['kind']=='preview')
        page.locator('#nextPageBtn').click()
        page.wait_for_function("previewTableBody.innerText.includes('B-2-INV-2')")
        select_account(page,'A-1')
        page.wait_for_function("previewTableBody.innerText.includes('A-1-INV-1')")
        assert page.locator('#previewLedgerInput').input_value()=='Cash <Alpha> — A-1'
        assert page.locator('#previewLedgerField b').count()==0
        previous=len([c for c in calls if c['kind']=='preview' and c['account']=='B-2' and c['page']==1])
        select_account(page,'B-2')
        page.wait_for_function("previewTableBody.innerText.includes('B-2-INV-1')")
        assert page.evaluate('currentPage')==1
        assert len([c for c in calls if c['kind']=='preview' and c['account']=='B-2' and c['page']==1])==previous
        assert all(c['params']['client_id']==['1'] and c['params']['template_id']==['1'] for c in calls)
        assert not errors
    finally: page.close()


def test_single_account_auto_selects_and_clear_retains_ledger_with_combined_filters(browser, static_server):
    page,calls=open_databank(browser,static_server,ledger_accounts=ACCOUNTS[:1])
    try:
        open_ledger(page)
        page.wait_for_function("previewTableBody.innerText.includes('A-1-INV-1')")
        assert page.locator('#previewLedgerInput').input_value()=='Cash <Alpha> — A-1'
        page.locator('#searchInput').fill('detail')
        page.wait_for_function("previewPageCache.size>=3")
        page.locator('#amountMin').fill('80');page.locator('#amountMax').fill('120')
        page.locator('#dataQualityFilter').select_option('complete')
        page.locator('#sundayOnlyCheckbox').check()
        page.wait_for_function("!isPreviewLoading")
        assert any(c['params'].get('search')==['detail'] and c['params'].get('amount_min')==['80']
            and c['params'].get('data_quality')==['complete'] and c['params'].get('sunday_only')==['true']
            for c in calls if c['kind']=='preview')
        page.locator('#modalDateRangeBtn').click()
        page.locator('#sharedRangeFrom').fill('2026-01-10')
        page.locator('#sharedRangeTo').fill('2026-01-31')
        with page.expect_response(lambda response:'/api/data/preview?' in response.url and 'date_start=2026-01-10' in response.url):
            page.locator('#sharedRangeConfirmBtn').click()
        page.wait_for_function("!isPreviewLoading")
        assert any(c['params'].get('date_start')==['2026-01-10'] and c['account']=='A-1'
            for c in calls if c['kind']=='preview')
        page.locator('#clearPreviewFiltersBtn').click()
        page.wait_for_function("!isPreviewLoading")
        assert page.evaluate('previewLedgerAccountCode')=='A-1'
        assert page.locator('#searchInput').input_value()==''
        assert page.locator('#amountMin').input_value()==''
        assert not page.locator('#sundayOnlyCheckbox').is_checked()
        before=len(calls)
        page.locator('#previewLedgerInput').fill('not an account')
        assert 'No matching uploaded accounts' in page.locator('#previewLedgerOptions').inner_text()
        page.locator('#previewLedgerInput').press('Enter')
        assert len(calls)==before and page.evaluate('previewLedgerAccountCode')==''
        page.locator('#previewLedgerInput').press('Escape')
        assert page.locator('#previewModal').is_visible()
        page.wait_for_function("previewLedgerAccountCode==='A-1' && !isPreviewLoading")
    finally:page.close()


@pytest.mark.parametrize('failure',['server','network','malformed','duplicate'])
def test_account_load_failures_clear_busy_state_and_retry_without_unscoped_preview(browser,static_server,failure):
    page,calls=open_databank(browser,static_server,ledger_accounts=ACCOUNTS[:1])
    retry=[False]
    def reply(route):
        if retry[0]:route.fulfill(json={'accounts':ACCOUNTS[:1]})
        elif failure=='server':route.fulfill(status=400,json={'detail':'Account source <b>unavailable</b>'})
        elif failure=='network':route.abort()
        elif failure=='duplicate':route.fulfill(json={'accounts':ACCOUNTS[:1]*2})
        else:route.fulfill(json={'accounts':None})
    page.route('**/api/data/ledger-accounts?*',reply)
    try:
        open_ledger(page)
        assert page.locator('#previewLedgerRetry').is_visible()
        assert page.locator('#previewLedgerInput').is_disabled()
        assert page.locator('#previewLedgerInput').get_attribute('aria-busy') is None
        assert page.locator('.preview-ledger-control .ui-field-spinner').count()==0
        assert page.evaluate('document.documentElement.style.cursor')==''
        assert not [c for c in calls if c['kind']=='preview']
        assert page.locator('#previewLedgerStatus b').count()==0
        retry[0]=True;page.locator('#previewLedgerRetry').click()
        page.wait_for_function("previewTableBody.innerText.includes('A-1-INV-1')")
        assert page.locator('#previewLedgerInput').is_enabled()
        assert page.locator('#previewLedgerStatus').is_hidden()
    finally:page.close()


@pytest.mark.parametrize('change',['template','close','client'])
def test_late_account_response_cannot_restore_old_context_or_busy_cursor(browser,static_server,change):
    page,calls=open_databank(browser,static_server,ledger_accounts=ACCOUNTS)
    waiting=[]
    page.route('**/api/data/ledger-accounts?*',lambda route:waiting.append(route))
    try:
        page.evaluate("() => { window.pendingOpen=openPreview(1,'Universal book'); }")
        page.wait_for_function("previewLedgerInput.getAttribute('aria-busy')==='true'")
        assert page.locator('#previewLedgerInput').is_disabled()
        assert page.evaluate('document.documentElement.style.cursor')=='progress'
        assert waiting
        if change=='template':
            page.evaluate("() => openPreview(2,'Ordinary')")
            page.wait_for_function("previewAccountState==='standard'")
        elif change=='close': page.locator('#closePreviewBtn').click()
        else: page.evaluate("() => { window.getSelectedClientId=()=>2; window.onClientChange(2); }")
        waiting[0].fulfill(json={'accounts':ACCOUNTS[:1]})
        page.evaluate('() => window.pendingOpen')
        assert page.evaluate('previewLedgerAccountCode')==''
        assert page.evaluate('document.documentElement.style.cursor')==''
        assert page.locator('.preview-ledger-control .ui-field-spinner').count()==0
        assert not [c for c in calls if c['kind']=='preview' and c['account']]
        if change=='template':assert page.locator('#previewLedgerField').is_hidden()
    finally:page.close()


def test_empty_uploaded_accounts_have_clear_guidance_and_no_preview_query(browser,static_server):
    page,calls=open_databank(browser,static_server,ledger_accounts=[])
    try:
        open_ledger(page)
        assert 'No ledgers have been uploaded' in page.locator('#previewLedgerStatus').inner_text()
        assert page.locator('#previewLedgerInput').is_disabled()
        assert not [c for c in calls if c['kind']=='preview']
    finally:page.close()


def test_late_rows_are_cached_for_their_own_account_without_overwriting_the_new_selection(browser,static_server):
    page,calls=open_databank(browser,static_server,ledger_accounts=ACCOUNTS)
    waiting=[]
    def defer_alpha(route):
        params=parse_qs(urlparse(route.request.url).query)
        if params.get('ledger_account_code')==['A-1'] and params.get('page')==['1']:
            waiting.append(route)
        else:route.fallback()
    page.route('**/api/data/preview?*',defer_alpha)
    try:
        open_ledger(page);select_account(page,'A-1')
        page.wait_for_function('isPreviewLoading')
        assert waiting
        select_account(page,'B-2')
        page.wait_for_function("previewTableBody.innerText.includes('B-2-INV-1')")
        waiting[0].fulfill(json=_preview_response(1,25,account='A-1'))
        page.wait_for_function("[...previewPageCache.keys()].some(k=>k.includes('ledger_account_code=A-1'))")
        assert 'B-2-INV-1' in page.locator('#previewTableBody').inner_text()
        select_account(page,'A-1')
        page.wait_for_function("previewTableBody.innerText.includes('A-1-INV-1')")
        assert len(waiting)==1
    finally:page.close()


def test_reopening_the_same_ledger_keeps_the_new_loading_owner_and_ignores_old_accounts(browser,static_server):
    page,_=open_databank(browser,static_server,ledger_accounts=ACCOUNTS)
    waiting=[]
    page.route('**/api/data/ledger-accounts?*',lambda route:waiting.append(route))
    try:
        page.evaluate("() => { window.oldOpen=openPreview(1,'Universal book'); }")
        page.wait_for_function("previewLedgerInput.getAttribute('aria-busy')==='true'")
        page.evaluate("() => { window.newOpen=openPreview(1,'Universal book'); }")
        page.wait_for_function("previewLedgerLoading!==null")
        page.wait_for_function("performance.getEntriesByType('resource').filter(e=>e.name.includes('/api/templates/1')).length>=1")
        # Route delivery is observed directly; no app/database state is involved.
        page.wait_for_timeout(50)
        assert len(waiting)==2
        waiting[0].fulfill(json={'accounts':ACCOUNTS[:1]})
        page.evaluate('() => window.oldOpen')
        assert page.locator('#previewLedgerInput').get_attribute('aria-busy')=='true'
        assert page.evaluate('document.documentElement.style.cursor')=='progress'
        waiting[1].fulfill(json={'accounts':ACCOUNTS[1:]})
        page.evaluate('() => window.newOpen')
        page.wait_for_function("previewTableBody.innerText.includes('B-2-INV-1')")
        assert page.evaluate('previewLedgerAccountCode')=='B-2'
        assert page.evaluate('document.documentElement.style.cursor')==''
    finally:page.close()


def test_template_metadata_failure_is_retryable_without_loading_unscoped_ledger_rows(browser,static_server):
    page,calls=open_databank(browser,static_server,ledger_accounts=ACCOUNTS[:1])
    ready=[False]
    def metadata(route):
        if ready[0]:route.fallback()
        else:route.fulfill(status=503,json={'detail':'Settings unavailable'})
    page.route('**/api/templates/1',metadata)
    try:
        page.evaluate("() => openPreview(1,'Universal book')")
        assert page.locator('#previewLedgerRetry').is_visible()
        assert not [c for c in calls if c['kind']=='preview']
        ready[0]=True;page.locator('#previewLedgerRetry').click()
        page.wait_for_function("previewTableBody.innerText.includes('A-1-INV-1')")
    finally:page.close()


def test_ledger_picker_keyboard_and_compact_responsive_toolbar(browser,static_server):
    page,_=open_databank(browser,static_server,ledger_accounts=ACCOUNTS)
    try:
        page.set_viewport_size({'width':1440,'height':1050});open_ledger(page)
        field=page.locator('#previewLedgerField').bounding_box()
        search=page.locator('#searchInput').bounding_box()
        assert abs(field['y']-search['y'])<5 and search['width']<340
        assert abs(field['y']-page.locator('#clearPreviewFiltersBtn').bounding_box()['y'])<5
        page.locator('#previewLedgerToggle').click()
        page.locator('#previewLedgerInput').press('ArrowUp')
        page.locator('#previewLedgerInput').press('Enter')
        page.wait_for_function("previewLedgerAccountCode==='B-2'")
        page.locator('#previewLedgerInput').press('ArrowDown')
        page.locator('#previewLedgerInput').press('Escape')
        assert page.locator('#previewModal').is_visible()
        assert page.locator('#previewLedgerOptions').is_hidden()
        page.set_viewport_size({'width':390,'height':844})
        page.locator('#previewLedgerToggle').click()
        box=page.locator('#previewLedgerOptions').bounding_box()
        assert box['x']>=0 and box['x']+box['width']<=390
        assert page.locator('#previewLedgerInput').is_visible()
    finally:page.close()


def test_classification_preview_keeps_its_own_filters_without_ledger_selector(browser,static_server):
    page,calls=open_databank(browser,static_server,ledger_accounts=ACCOUNTS)
    page.route('**/api/data/cashflows-classification-preview?*',
        lambda route:route.fulfill(json=_preview_response(1,50)))
    try:
        page.evaluate("() => openClassificationPreview(1,'Transactions')")
        page.wait_for_function("previewTableBody.innerText.includes('INV-1')")
        assert page.locator('#previewLedgerField').is_hidden()
        assert page.locator('#cashflowsClassificationFilters').is_visible()
        assert not [c for c in calls if c['kind']=='accounts']
        assert page.evaluate("buildPreviewRequestSpec().params.has('ledger_account_code')") is False
    finally:page.close()
