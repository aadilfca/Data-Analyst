import json
import copy
from io import BytesIO
from pathlib import Path
import logging
from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd
from openpyxl import load_workbook
import pytest


def test_comparative_statement_browser_isolated_sources_and_review(tmp_path):
    """Actual report DOM with synthetic accounting/API data; no live server/DB."""
    import functools
    import os
    import threading
    from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
    from urllib.parse import urlparse, parse_qs
    from playwright.sync_api import sync_playwright, expect
    from test_balance_sheet import comparative_case, transfer_case
    from services.financial_statement_balances import build_balance_sheet, build_income_statement

    data, options = comparative_case.__wrapped__(transfer_case.__wrapped__())
    upload = dict(id=55, file_name='Synthetic.xlsx', period_start='2026-01-01', period_end='2026-06-30')

    def payload(changed=None, chosen=55, income=False):
        builder = build_income_statement if income else build_balance_sheet
        result = builder(*(changed or data), **options)
        if not income:
            result['comparative'].update(opening_at='2026-01-01', source_label='TB opening balances')
        section = result['statement_section']
        source = next(row for row in result['categories'] if row.get('row_type') == 'category' and row.get('statement_section') == section)
        note_id = 702 if income else 701
        result['note_references'] = {'statement_section': section, 'roots': [
            {'id': note_id, 'source_statement_category_id': source['id'], 'sort_order': 0}
        ]}
        result['notes_context_key'] = section + '-notes-context'
        return dict(client_id=7, upload=upload | {'id': chosen}, context_key='same', **result)

    def notes_payload(chosen=55, income=False):
        core = payload(chosen=chosen, income=income)
        section = core['statement_section']
        reference = core['note_references']['roots'][0]
        note = {
            **reference, 'label': 'Synthetic note', 'parent_category_id': None, 'is_active': True,
            'current_amount': '300', 'comparative_amount': '250' if not income else None,
            'account_count': 1, 'children': [{
                'id': int(reference['id']) + 1000, 'parent_category_id': reference['id'],
                'source_statement_category_id': reference['source_statement_category_id'],
                'sort_order': 0, 'label': 'Synthetic detail', 'is_active': True,
                'current_amount': '300', 'comparative_amount': '250' if not income else None,
                'account_count': 1, 'children': [],
            }],
        }
        second_note_id = int(reference['id']) + 1
        second_note = {
            **reference, 'id': second_note_id, 'sort_order': 1, 'label': 'Second synthetic note',
            'current_amount': '75', 'comparative_amount': '50' if not income else None,
            'account_count': 1, 'children': [{
                'id': second_note_id + 1000, 'parent_category_id': second_note_id,
                'source_statement_category_id': reference['source_statement_category_id'],
                'sort_order': 0, 'label': 'Second synthetic detail', 'is_active': True,
                'current_amount': '75', 'comparative_amount': '50' if not income else None,
                'account_count': 1, 'children': [],
            }],
        }
        return {
            'client_id': core['client_id'], 'upload': core['upload'], 'context_key': core['context_key'],
            'notes_context_key': core['notes_context_key'], 'statement_section': section,
            'categories': core['categories'], 'calculations': core['calculations'],
            'comparative': core.get('comparative'),
            'notes': {'statement_section': section, 'roots': [note, second_note], 'coverage': [{
                'statement_category_id': reference['source_statement_category_id'], 'label': 'Synthetic category',
                'note_root_id': reference['id'], 'account_count': 1, 'assigned_account_count': 1,
                'unassigned_account_count': 0, 'current_difference': '0',
                'comparative_difference': '0' if not income else None,
            }]},
        }

    state = dict(value=payload(), error=False, notes_error=False)
    calls, errors, notes_uploads = [], [], []

    class QuietHandler(SimpleHTTPRequestHandler):
        def log_message(self, *args): pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), functools.partial(QuietHandler, directory=str(Path(__file__).resolve().parents[1])))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_page(viewport={'width': 1440, 'height': 1000})
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.add_init_script("""(() => { const original = window.fetch.bind(window); window.__notesStartedAfterMain = false; window.fetch = function (input, init) { const url = typeof input === 'string' ? input : input.url; if (url.includes('/financial-statements/notes')) { const table = document.querySelector('#balanceSheetTable'); const view = document.querySelector('#balanceSheetView'); window.__notesStartedAfterMain = !!table && !table.hidden && view.getAttribute('aria-busy') === 'false'; } return original(input, init); }; })();""")

            def api(route):
                parsed = urlparse(route.request.url)
                calls.append((parsed.path, route.request.method))
                if parsed.path.endswith('/auth/current-client'):
                    value = dict(client_id=7, client_name='Example Manufacturing', username='Analyst')
                elif parsed.path.endswith('/users'):
                    value = []
                elif parsed.path.endswith('/account-control-center/uploads'):
                    value = {'uploads': [upload, upload | {'id': 56, 'file_name': 'Second.xlsx'}]}
                elif parsed.path.endswith('/financial-statements/notes'):
                    if state['notes_error']:
                        route.fulfill(status=503, json={'detail': 'Synthetic notes unavailable'})
                        return
                    query = parse_qs(parsed.query)
                    section = query.get('statement_section', ['balance_sheet'])[0]
                    chosen = int(query.get('trial_balance_upload_id', ['55'])[0])
                    notes_uploads.append((section, chosen))
                    value = notes_payload(chosen=chosen, income=section == 'income_statement')
                elif parsed.path.endswith('/financial-statements/income-statement'):
                    chosen = int(parse_qs(parsed.query).get('trial_balance_upload_id', ['55'])[0])
                    value = payload(chosen=chosen, income=True)
                elif parsed.path.endswith('/financial-statements/balance-sheet'):
                    if state['error']:
                        route.fulfill(status=409, json={'detail': 'Synthetic source unavailable'})
                        return
                    chosen = int(parse_qs(parsed.query)['trial_balance_upload_id'][0])
                    value = copy.deepcopy(state['value'])
                    value['upload']['id'] = chosen
                else:
                    value = {'settings': {}, 'categories': [], 'calculations': []}
                route.fulfill(status=200, json=value)

            page.route('**/api/**', api)
            base = 'http://127.0.0.1:' + str(server.server_port)
            page.goto(base + '/static/reconciliation.html?report=balance-sheet&trial_balance_upload_id=55')
            expect(page.locator('#balanceSheetTable')).to_be_visible()
            page.wait_for_function('window.__notesStartedAfterMain === true')
            notes_path = '/api/reconciliation/financial-statements/notes'
            assert sum(path == notes_path for path, _ in calls) == 1
            page.locator('#balanceSheetNotesOpen').click()
            expect(page.locator('#balanceSheetNotesTable')).to_be_visible()
            expect(page.locator('#balanceSheetNotesRows')).to_contain_text('Synthetic note')
            expect(page.locator('#balanceSheetNotesHeading')).to_have_text('Example Manufacturing : Notes to Balance Sheet')
            expect(page.locator('#balanceSheetNotesPeriod')).to_have_text('Jan 01, 2026 to Jun 30, 2026')
            expect(page.locator('#balanceSheetNotesFinancialStatements')).to_be_visible()
            assert page.locator('#balanceSheetNotesView .balance-sheet-caption').count() == 0
            roots = page.locator('#balanceSheetNotesTable tr.bs-note-root')
            expect(roots).to_have_count(2)
            assert roots.nth(0).locator('td.balance-sheet-amount').all_text_contents() == ['', '']
            assert page.locator('#balanceSheetNotesNote-1701 td.balance-sheet-amount').all_text_contents() == ['300', '250']
            assert page.locator('#balanceSheetNotesTable tr.bs-note-subtotal').nth(0).locator('td.balance-sheet-amount').all_text_contents() == ['300', '250']
            assert page.locator('#balanceSheetNotesTable tr.bs-note-subtotal').nth(1).locator('td.balance-sheet-amount').all_text_contents() == ['75', '50']
            group_gap = page.locator('#balanceSheetNotesTable tr.bs-note-group-gap')
            expect(group_gap).to_have_count(1)
            assert group_gap.bounding_box()['height'] >= 18
            row_order = page.locator('#balanceSheetNotesRows').evaluate("el => Array.from(el.children).map(row => row.id || row.className)")
            assert row_order.index('bs-note-subtotal') < row_order.index('bs-note-group-gap') < row_order.index('balanceSheetNotesNote-702')
            for width in (1440, 390):
                page.set_viewport_size({'width': width, 'height': 1000})
                assert page.locator('#balanceSheetNotesToolbar').evaluate('el => el.getBoundingClientRect().right <= innerWidth + 1')
            page.set_viewport_size({'width': 1440, 'height': 1000})
            assert sum(path == notes_path for path, _ in calls) == 1
            page.locator('#balanceSheetNotesBack').click()

            early = browser.new_page(viewport={'width': 1440, 'height': 1000})
            early.on('pageerror', lambda error: errors.append(str(error)))
            early.add_init_script("""(() => { const original = window.fetch.bind(window); window.__releaseMainStatementFetch = null; window.fetch = function (input, init) { const url = typeof input === 'string' ? input : input.url; if (url.includes('/financial-statements/balance-sheet') && url.includes('include_notes=false')) { return new Promise((resolve, reject) => { window.__releaseMainStatementFetch = () => original(input, init).then(resolve, reject); }); } return original(input, init); }; })();""")
            early.route('**/api/**', api)
            early.goto(base + '/static/reconciliation.html?report=balance-sheet&trial_balance_upload_id=55')
            early.wait_for_function('window.__releaseMainStatementFetch !== null')
            assert early.locator('#balanceSheetTable').is_hidden()
            early.locator('#balanceSheetNotesOpen').click()
            expect(early.locator('#balanceSheetNotesTable')).to_be_visible()
            expect(early.locator('#balanceSheetNotesRows')).to_contain_text('Synthetic note')
            assert sum(path == notes_path for path, _ in calls) == 2
            early.evaluate('window.__releaseMainStatementFetch()')
            early.wait_for_function("document.querySelector('#balanceSheetView').getAttribute('aria-busy') === 'false'")
            assert sum(path == notes_path for path, _ in calls) == 2
            assert early.locator('#balanceSheetNotesView').is_visible()
            early.close()

            current = page.locator('#balanceSheetReconciliation')
            opening = page.locator('#openingBalanceSheetReconciliation')
            expect(opening).to_be_visible()
            expect(page.locator('#statementComparativeHeader')).to_contain_text('2026-01-01')
            page.locator('#balanceSheetDiagnosticsOpen').click()
            expect(page.locator('#balanceSheetDiagnostics')).to_contain_text('Comparative basis: TB opening balances')
            page.locator('#balanceSheetDiagnosticsDialog [data-statement-dialog-close]').click()
            assert not opening.evaluate('el => el.open')
            expect(current.locator('summary')).to_contain_text('opening I/S items need review')
            expect(opening.locator('summary')).to_contain_text('opening I/S items need review')
            review = page.locator('#balanceSheetRows .opening-income-review')
            expect(review).to_have_count(1)
            assert review.locator('td').all_text_contents() == ['', '', '-20']
            capital = page.locator('#balanceSheetRows tr.bs-category').filter(has=page.locator('th', has_text='Category 3'))
            assert capital.locator('td').all_text_contents() == ['', '1,200', '900']
            review.locator('summary').click()
            expect(review.locator('li')).to_contain_text('COST')
            expect(review.locator('li')).to_contain_text('Opening Dr 20')
            current.locator('summary').click()
            opening.locator('summary').focus()
            page.keyboard.press('Enter')
            assert opening.evaluate('el => el.open')
            expect(page.locator('#statementReconciliationIssues')).to_contain_text('opening (20) + movement 320 = closing 300')
            opening_profit = page.locator('#openingStatementReconciliationRows tr').filter(has_text='Opening I/S items')
            assert opening_profit.locator('td').all_text_contents() == ['20', '20']
            for width in (1440, 390):
                page.set_viewport_size({'width': width, 'height': 1000})
                assert page.locator('#balanceSheetView').evaluate('el => el.getBoundingClientRect().right <= innerWidth + 1')
                assert capital.locator('th').bounding_box()['width'] >= (120 if width <= 600 else 200)
                page.evaluate('window.scrollTo(0, 0)')
                page.screenshot(path=str(tmp_path / ('comparative-' + str(width) + '.png')), full_page=True)

            # Source changes clear expanded evidence, and both columns reload together.
            page.locator('#balanceSheetUpload').select_option('56')
            expect(page.locator('#balanceSheetUpload')).to_be_enabled()
            expect(opening).to_be_visible()
            assert not opening.evaluate('el => el.open')
            changed = copy.deepcopy(data)
            changed[0][0]['opening_dr'] = '1000'
            changed[0][3]['opening_cr'] = '20'
            state['value'] = payload(changed)
            page.locator('#balanceSheetReload').click()
            expect(page.locator('#balanceSheetReload')).to_be_enabled()
            expect(page.locator('.opening-income-review td').last).to_have_text('0')
            page.locator('#balanceSheetNotesOpen').click()
            expect(page.locator('#balanceSheetNotesTable')).to_be_visible()
            expect(page.locator('#balanceSheetNotesPeriod')).to_have_text('Jan 01, 2026 to Jun 30, 2026')
            assert ('balance_sheet', 56) in notes_uploads
            assert parse_qs(urlparse(page.url).query)['trial_balance_upload_id'] == ['56']
            page.locator('#balanceSheetNotesFinancialStatements').click()
            expect(page.locator('#financialStatementsView')).to_be_visible()
            assert parse_qs(urlparse(page.url).query)['trial_balance_upload_id'] == ['56']
            page.locator('#incomeStatementCard').click()
            expect(page.locator('#balanceSheetTable')).to_be_visible()
            expect(page.locator('#balanceSheetUpload')).to_have_value('56')
            page.locator('#incomeStatementNotesOpen').click()
            expect(page.locator('#incomeStatementNotesTable')).to_be_visible()
            expect(page.locator('#incomeStatementNotesRows')).to_contain_text('Synthetic note')
            assert ('income_statement', 56) in notes_uploads
            assert parse_qs(urlparse(page.url).query)['trial_balance_upload_id'] == ['56']
            page.locator('#incomeStatementNotesBack').click()
            expect(page.locator('#balanceSheetTable')).to_be_visible()
            assert parse_qs(urlparse(page.url).query)['trial_balance_upload_id'] == ['56']
            page.locator('#balanceSheetBack').click()
            expect(page.locator('#financialStatementsView')).to_be_visible()
            page.locator('#balanceSheetCard').click()
            expect(page.locator('#openingBalanceSheetReconciliation')).to_be_visible()
            expect(page.locator('#balanceSheetUpload')).to_have_value('56')
            page.locator('.opening-income-review summary').click()
            expect(page.locator('.opening-income-review li')).to_have_count(2)
            changed = copy.deepcopy(data)
            changed[0][0]['opening_dr'] = '979'
            state['value'] = payload(changed)
            page.locator('#balanceSheetReload').click()
            expect(opening.locator('summary')).to_contain_text('source imbalance remains')
            expect(current.locator('summary')).not_to_contain_text('source imbalance remains')
            broken = copy.deepcopy(data)
            broken[1] = [row for row in broken[1] if row['account_code'] != 'COST']
            state['value'] = payload(broken)
            page.locator('#balanceSheetReload').click()
            expect(opening.locator('summary')).to_contain_text('Not reconciled')
            state['error'] = True
            page.locator('#balanceSheetReload').click()
            expect(page.locator('#balanceSheetStatus')).to_contain_text('Synthetic source unavailable')
            expect(current).to_be_hidden()
            expect(opening).to_be_hidden()
            page.goto(base + '/static/reconciliation.html?report=income-statement&trial_balance_upload_id=55')
            expect(page.locator('#balanceSheetTable')).to_be_visible()
            expect(page.locator('#statementComparativeHeader')).to_be_hidden()
            expect(current).to_be_hidden()
            expect(opening).to_be_hidden()
            expect(page.locator('.opening-income-review')).to_have_count(0)

            # A direct Notes URL should load the requested Income Statement note.
            page.goto(base + '/static/reconciliation.html?report=income-statement-notes&trial_balance_upload_id=55&note_id=702')
            expect(page.locator('#incomeStatementNotesTable')).to_be_visible()
            expect(page.locator('#incomeStatementNotesRows')).to_contain_text('Synthetic note')
            expect(page.locator('#incomeStatementNotesHeading')).to_have_text('Example Manufacturing : Notes to Income Statement')
            expect(page.locator('#incomeStatementNotesPeriod')).to_have_text('Jan 01, 2026 to Jun 30, 2026')
            assert page.locator('#incomeStatementNotesView .balance-sheet-caption').count() == 0
            income_roots = page.locator('#incomeStatementNotesTable tr.bs-note-root')
            expect(income_roots).to_have_count(2)
            assert income_roots.nth(0).locator('td.balance-sheet-amount').all_text_contents() == ['']
            assert page.locator('#incomeStatementNotesNote-1702 td.balance-sheet-amount').all_text_contents() == ['300']
            assert page.locator('#incomeStatementNotesNote-1703 td.balance-sheet-amount').all_text_contents() == ['75']
            assert page.locator('#incomeStatementNotesTable tr.bs-note-subtotal').nth(0).locator('td.balance-sheet-amount').all_text_contents() == ['300']
            assert page.locator('#incomeStatementNotesTable tr.bs-note-subtotal').nth(1).locator('td.balance-sheet-amount').all_text_contents() == ['75']
            expect(page.locator('#incomeStatementNotesTable tr.bs-note-group-gap')).to_have_count(1)
            for width in (1440, 390):
                page.set_viewport_size({'width': width, 'height': 1000})
                assert page.locator('#incomeStatementNotesToolbar').evaluate('el => el.getBoundingClientRect().right <= innerWidth + 1')
            page.set_viewport_size({'width': 1440, 'height': 1000})
            assert page.locator('#incomeStatementNotesNote-702').evaluate("el => el.classList.contains('bs-note-selected')")

            # A failed background load must leave the statement usable, and reopening Notes retries.
            state['error'] = False
            state['notes_error'] = True
            page.goto(base + '/static/reconciliation.html?report=balance-sheet&trial_balance_upload_id=55')
            expect(page.locator('#balanceSheetTable')).to_be_visible()
            page.locator('#balanceSheetNotesOpen').click()
            expect(page.locator('#balanceSheetNotesStatus')).to_contain_text('Synthetic notes unavailable')
            state['notes_error'] = False
            page.locator('#balanceSheetNotesBack').click()
            page.locator('#balanceSheetNotesOpen').click()
            expect(page.locator('#balanceSheetNotesTable')).to_be_visible()
            expect(page.locator('#balanceSheetNotesRows')).to_contain_text('Synthetic note')
            page.locator('#balanceSheetNotesFinancialStatements').click()
            expect(page.locator('#financialStatementsView')).to_be_visible()

            assert not errors, errors
            assert all(method == 'GET' for _, method in calls)
            destination = os.environ.get('CODEX_COMPARATIVE_SCREENSHOT_DIR')
            if destination:
                import shutil
                for picture in tmp_path.glob('comparative-*.png'):
                    shutil.copyfile(picture, Path(destination) / picture.name)
            browser.close()
    finally:
        server.shutdown()
        server.server_close()

from routers import reconciliation
from services.ledger_group_summary import build_cashflow_summary, build_financial_statement_summary
from services.ledger_report_source import CASHFLOW_CHILD_DETAILS_VERSION, CASHFLOW_DRILLDOWN_DETAILS_VERSION, CASHFLOW_SUMMARY_PAYLOAD_VERSION, load_cashflow_prepared_drilldown, load_cashflow_prepared_rollup_summary
from routers import settings as settings_router
from services.reconciliation_xlsx import export_cashflow_xlsx
from test_reconciliation_balance_source import independent_year_boundary_case, horizontal_reconciliation_case, selected_account_case


def test_adjustment_section_totals_aggregate_before_rounding():
    from services.reconciliation_balance_source import cashflow_adjustment_sections
    sections=cashflow_adjustment_sections([dict(receipts='0.004',payments='-0.004')]*2)
    assert sections=={'receipts':{'add':0.01,'less':0.0},'payments':{'add':0.0,'less':-0.01}}


def test_compact_adjustments_preserve_precision_and_distinct_voucher_membership():
    from services.reconciliation_balance_source import compact_cashflow_adjustment_groups
    entry=dict(treasury_view='combined',reason='refund',label='Refund',category='Welfare',
        voucher_id='CPV-1',period_key='2026-01',receipts='0.004',payments='-0.004',private_source='not retained')
    groups=compact_cashflow_adjustment_groups({'key':[entry,entry,entry|{'voucher_id':'CPV-2'}]})
    assert {g['side']:g['adjustment_total'] for g in groups}=={'receipts':0.01,'payments':-0.01}
    assert all(g['entries']==[dict(voucher_id='CPV-1',period_key='2026-01'),dict(voucher_id='CPV-2',period_key='2026-01')] for g in groups)
    assert 'private_source' not in str(groups) and '0.004' not in str(groups)


def test_group_bundle_and_oversized_pages_reuse_completed_paper(selected_report_env,monkeypatch):
    env=selected_report_env
    parent=env.client.get('/api/reconciliation/cashflows/reconciliation',params=env.params).json()
    row=next(r for r in parent['views']['combined']['adjustments'] if r['total']['receipts'])
    query=env.params|dict(proof='combined',support_key=row['key'],side='receipts',expected_identity=parent['cache_identity'])
    calls=[]
    def bundle(*args,**kwargs):
        calls.append((args,kwargs));assert len(args[3])==1
        return dict(complete=False,reason='row_limit',rows=[],columns=[],groups=[])
    def page(*args,**kwargs):
        calls.append((args,kwargs))
        return dict(source_book=kwargs['source_book'],search=kwargs['search'],rows=[],columns=[],total_rows=1001,
            page=kwargs['page'],page_count=21,limit=50)
    def prohibited(*args,**kwargs):raise AssertionError('Warm popup must not reload or recalculate the summary')
    monkeypatch.setattr(reconciliation,'load_cashflow_adjustment_bundle',bundle)
    monkeypatch.setattr(reconciliation,'load_cashflow_adjustment_rows',page)
    monkeypatch.setattr(reconciliation,'build_cashflow_summary',prohibited)
    monkeypatch.setattr(reconciliation,'load_ledger_report_source',prohibited)
    monkeypatch.setattr(reconciliation,'load_cashflow_adjustment_treasury_rows',prohibited)
    base='/api/reconciliation/cashflows/reconciliation/'
    value=env.client.get(base+'adjustment-bundle',params=query).json()
    assert value['bundle_schema']==2 and value['support_key']==row['key'] and not value['complete']
    for book in ('ledger','voucher'):
        for number in (1,2,3,2):
            response=env.client.get(base+'adjustment-transactions',params=query|dict(source_book=book,page=number))
            assert response.status_code==200,response.text
            assert response.json()['adjustment_total']==row['total']['receipts']
    assert len(calls)==9
    assert env.client.get(base+'adjustment-bundle',params=query|dict(side='payments',proof='bank')).status_code==400
    env.stamp[0]='b'*64
    assert env.client.get(base+'adjustment-transactions',params=query).status_code==409
    assert len(calls)==9


def test_membership_expiry_rebuilds_once_and_isolates_signed_in_owner(selected_report_env,monkeypatch):
    env=selected_report_env
    clock=[0]
    monkeypatch.setattr(reconciliation,'monotonic',lambda:clock[0])
    parent=env.client.get('/api/reconciliation/cashflows/reconciliation',params=env.params).json()
    row=next(r for r in parent['views']['combined']['adjustments'] if r['total']['receipts'])
    query=env.params|dict(support_key=row['key'],side='receipts',expected_identity=parent['cache_identity'])
    def treasury(*args,**kwargs):return [r for r in env.case['source']['transaction_rows'] if r['account_code'] in args[2]]
    monkeypatch.setattr(reconciliation,'load_cashflow_adjustment_treasury_rows',treasury)
    monkeypatch.setattr(reconciliation,'load_cashflow_adjustment_rows',lambda *a,**k:dict(rows=[],columns=[],source_book=k['source_book'],search=k['search']))
    before=len(env.source_calls)
    url='/api/reconciliation/cashflows/reconciliation/adjustment-transactions'
    clock[0]=601
    for number in (1,2,3):
        response=env.client.get(url,params=query|dict(page=number))
        assert response.status_code==200,response.text
        assert response.json()['adjustment_total']==row['total']['receipts']
    assert len(env.source_calls)==before+1
    env.client.app.dependency_overrides[reconciliation.require_admin]=lambda:{'user_id':2}
    assert env.client.get(url,params=query).status_code==200
    assert len(env.source_calls)==before+2
    env.client.app.dependency_overrides[reconciliation.require_admin]=lambda:{'user_id':1}
    assert env.client.get(url,params=query).status_code==200
    assert len(env.source_calls)==before+2


def test_failed_paper_snapshot_cannot_publish_membership(selected_report_env,monkeypatch):
    env=selected_report_env
    original=reconciliation.run_read_snapshot_pg
    def fail(callback):
        original(callback)
        raise RuntimeError('snapshot did not commit its read')
    monkeypatch.setattr(reconciliation,'run_read_snapshot_pg',fail)
    with pytest.raises(RuntimeError,match='snapshot did not'):
        env.client.get('/api/reconciliation/cashflows/reconciliation',params=env.params)
    assert not reconciliation._adjustment_memberships.entries


@pytest.mark.parametrize('selection,view', [(None,'combined'),(['C1'],'combined'),(['B1','C1'],'split')])
def test_flat_adjustment_reader_matches_paper_without_balances_or_comparisons(selected_report_env, monkeypatch, selection, view):
    env=selected_report_env
    params=env.params|{'treasury_view':view}
    if selection: params['selected_treasury_account_codes']=selection
    parent=env.client.get('/api/reconciliation/cashflows/reconciliation',params=params).json()
    def treasury(*args,**kwargs):
        assert env.active==[True]
        codes=args[2]
        return [r for r in env.case['source']['transaction_rows'] if r['account_code'] in codes]
    captured=[]
    def rows(*args,**kwargs):
        assert env.active==[True]
        captured.append((args,kwargs))
        return dict(rows=[],columns=[],total_rows=0,page=1,page_count=1,limit=50,source_book=kwargs['source_book'],search=kwargs['search'])
    monkeypatch.setattr(reconciliation,'load_cashflow_adjustment_treasury_rows',treasury)
    monkeypatch.setattr(reconciliation,'load_cashflow_adjustment_rows',rows)
    def prohibited(*args,**kwargs): raise AssertionError('Full reconciliation rebuild is prohibited for flat detail')
    monkeypatch.setattr(reconciliation,'_cashflow_reconciliation_snapshot',prohibited)
    monkeypatch.setattr(reconciliation,'load_cashflow_difference_source',prohibited)
    monkeypatch.setattr(reconciliation,'build_cashflow_reconciliation',prohibited)
    monkeypatch.setattr(reconciliation,'build_cashflow_summary',prohibited)
    url='/api/reconciliation/cashflows/reconciliation/adjustment-transactions'
    count=0
    for proof,part in parent['views'].items():
        for adjustment in part['adjustments']:
            for side in ('receipts','payments'):
                if not adjustment['total'][side]: continue
                query=params|dict(support_key=adjustment['key'],side=side,expected_identity=parent['cache_identity'])
                response=env.client.get(url,params=query)
                assert response.status_code==200,response.text
                detail=response.json()
                assert detail['adjustment_total']==adjustment['total'][side]
                assert detail['proof']==proof
                assert captured[-1][0][3] and all(set(entry)=={'voucher_id','period_key'} for entry in captured[-1][0][3])
                assert env.client.get(url,params=query|{'expected_identity':'f'*64}).status_code==409
                assert env.client.get(url,params=query|{'side':'invalid'}).status_code==400
                assert env.client.get(url,params=query|{'limit':201}).status_code==422
                assert env.client.get(url,params=query|{'support_key':'x'*24}).status_code==400
                count+=1
    assert count


@pytest.mark.parametrize('selection,view', [(None,'combined'),(['C1'],'combined'),(['B1','C1'],'split')])
def test_adjustment_bundle_uses_one_snapshot_build_and_matches_all_paper_groups(selected_report_env, monkeypatch, selection, view):
    env=selected_report_env
    params=env.params|{'treasury_view':view}
    if selection:params['selected_treasury_account_codes']=selection
    parent=env.client.get('/api/reconciliation/cashflows/reconciliation',params=params).json()
    captured=[]
    def treasury(*args,**kwargs):
        assert env.active==[True]
        return [r for r in env.case['source']['transaction_rows'] if r['account_code'] in args[2]]
    def bundle(*args,**kwargs):
        assert env.active==[True]
        captured.append((args,kwargs))
        return dict(complete=True,rows=[],columns=[],groups=[{k:v for k,v in g.items() if k!='entries'} for g in args[3]])
    monkeypatch.setattr(reconciliation,'load_cashflow_adjustment_treasury_rows',treasury)
    monkeypatch.setattr(reconciliation,'load_cashflow_adjustment_bundle',bundle)
    def prohibited(*args,**kwargs):raise AssertionError('Preload must not rebuild balances or posting comparisons')
    monkeypatch.setattr(reconciliation,'_cashflow_reconciliation_snapshot',prohibited)
    monkeypatch.setattr(reconciliation,'load_cashflow_difference_source',prohibited)
    monkeypatch.setattr(reconciliation,'build_cashflow_reconciliation',prohibited)
    monkeypatch.setattr(reconciliation,'build_cashflow_summary',prohibited)
    url='/api/reconciliation/cashflows/reconciliation/adjustment-bundle'
    for proof,part in parent['views'].items():
        before=len(env.source_calls)
        query=params|dict(proof=proof,expected_identity=parent['cache_identity'])
        response=env.client.get(url,params=query)
        assert response.status_code==200,response.text
        value=response.json()
        assert value['complete'] and value['bundle_schema']==1 and value['proof']==proof
        assert len(env.source_calls)==before
        assert env.source_calls[-1].get('selected_treasury_account_codes')==selection
        groups={(g['key'],g['side']):g for g in captured[-1][0][3]}
        for row in part['adjustments']:
            for side in ('receipts','payments'):
                if row['total'][side]:assert groups[(row['key'],side)]['adjustment_total']==row['total'][side]
        assert all(set(e)=={'voucher_id','period_key'} for g in groups.values() for e in g['entries'])
        assert env.client.get(url,params=query|{'expected_identity':'f'*64}).status_code==409
        assert env.client.get(url,params=query|{'proof':'invalid'}).status_code==400
        assert env.client.get(url,params=query|{'source_book':'invalid'}).status_code==400
        monkeypatch.setattr(reconciliation,'CASHFLOW_ADJUSTMENT_BUNDLE_MAX_BYTES',1)
        limited=env.client.get(url,params=query).json()
        assert not limited['complete'] and limited['reason']=='byte_limit' and limited['rows']==[] and limited['groups']==[]
        monkeypatch.setattr(reconciliation,'CASHFLOW_ADJUSTMENT_BUNDLE_MAX_BYTES',1048576)


@pytest.fixture
def selected_report_env(selected_account_case, monkeypatch):
    """Exercise real API/accounting/export code against independent fixture books."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from services.reconciliation_balance_source import build_uploaded_ledger_balances
    monkeypatch.setattr(reconciliation, '_adjustment_memberships', reconciliation._AdjustmentMembershipCache())
    case = selected_account_case
    config = dict(source_template_id=1, bank_account_codes=[c for c,k in case['accounts'].items() if k=='Bank'],
        cash_account_codes=[c for c,k in case['accounts'].items() if k=='Cash'], cashflow_report_revision=7,
        receipt_presentation='net', match_internal_transfers=True, customer_payment_roles=case['roles'],
        category_mappings={}, vendor_account_codes=['CON'])
    active, source_calls, evidence_calls, identity_calls, source_snapshots = [], [], [], [], []
    stamp = ['a'*64]
    def snapshot(callback):
        assert not active
        active.append(True)
        try: return callback(None)
        finally: active.pop()
    def report(*args, **kwargs):
        source_calls.append(kwargs)
        source_snapshots.append(bool(active))
        return SimpleNamespace(journal=case['journal'], coa=case['coa'], ready=True,
            diagnostics=(), metadata={'source_row_count':len(case['journal']), 'cashflow_report_source_key':'fixture'})
    def evidence(client, template, codes, *args, **kwargs):
        assert active == [True]
        evidence_calls.append(codes)
        source = copy.deepcopy(case['source'])
        source['uploads'] = [r for r in source['uploads'] if r['ledger_account_code'] in codes]
        source['ledger_rows'] = [r for r in source['ledger_rows'] if r['account_code'] in codes]
        references = {r['voucher_id'] for r in source['transaction_rows'] if r['account_code'] in codes}
        source['transaction_rows'] = [r for r in source['transaction_rows'] if r['voucher_id'] in references]
        return source
    def identity(client, template, codes, **kwargs):
        assert active == [True]
        identity_calls.append(codes)
        return stamp[0]
    def balances(client, codes, periods, movements, **kwargs):
        assert active == [True]
        return build_uploaded_ledger_balances(case['source']['uploads'], case['source']['ledger_rows'],
            codes, periods, movements, account_names=kwargs['account_names'], treasury_movements=kwargs['treasury_movements'])
    monkeypatch.setattr(reconciliation, '_require_active_client', lambda *args: None)
    monkeypatch.setattr(reconciliation, '_cashflow_configuration', lambda *args: config)
    monkeypatch.setattr(reconciliation, '_latest_trial_balance_accounts', lambda *args: [])
    monkeypatch.setattr(reconciliation, 'load_ledger_report_source', report)
    monkeypatch.setattr(reconciliation, 'load_cashflow_difference_source', evidence)
    monkeypatch.setattr(reconciliation, 'load_cashflow_reconciliation_identity', identity)
    monkeypatch.setattr(reconciliation, 'load_uploaded_ledger_balances', balances)
    monkeypatch.setattr(reconciliation, 'run_read_snapshot_pg', snapshot)
    app = FastAPI(); app.include_router(reconciliation.router)
    app.dependency_overrides[reconciliation.require_admin] = lambda: {'user_id':1}
    with TestClient(app) as client:
        yield SimpleNamespace(client=client, case=case, config=config, active=active,
            source_calls=source_calls, evidence_calls=evidence_calls, identity_calls=identity_calls, stamp=stamp,
            source_snapshots=source_snapshots,
            params=dict(client_id=7, period_start='2026-01-01', period_end='2026-01-31'))


@pytest.mark.parametrize('selection', [['C1'], ['C2'], ['C1','C2'], ['C2','C3'], ['B1'], ['B1','C1']])
@pytest.mark.parametrize('view', ['combined','bank','cash','split'])
def test_selected_api_balances_reconciliation_details_and_excel_match_independent_books(selected_report_env, selection, view):
    env = selected_report_env
    params = env.params | dict(selected_treasury_account_codes=selection, treasury_view=view)
    base = '/api/reconciliation/cashflows'
    selected = [code for code in selection if view in ('combined','split') or env.case['accounts'][code].lower()==view]
    response = env.client.get(base+'/preview', params=params|dict(include_balances=True, warm_ledger_cache=True))
    if not selected:
        assert response.status_code == 400
        assert env.source_calls == []
        return
    assert response.status_code == 200, response.text
    report = response.json(); preview = report['preview']
    assert report['account_selection']['account_codes'] == sorted(selected)
    assert preview['account_selection'] == report['ledger_context']['account_selection']
    assert env.source_calls[-1]['selected_treasury_account_codes'] == sorted(selected)
    assert env.source_calls[-1]['treasury_account_codes'] == env.config['cash_account_codes']+env.config['bank_account_codes']
    rows = [r for r in env.case['source']['ledger_rows'] if r['account_code'] in selected]
    receipt = sum(float(r['debit']) for r in rows); payment = sum(float(r['credit']) for r in rows)
    balance = preview['balance_reconciliation']['overall']
    assert balance['opening_total'] == 10000*len(selected)
    assert balance['closing_total'] == pytest.approx(10000*len(selected)+receipt-payment)
    paper_response = env.client.get(base+'/reconciliation', params=params)
    assert paper_response.status_code == 200, paper_response.text
    paper = paper_response.json()
    assert paper['account_selection']['account_codes'] == sorted(selected)
    assert set(env.evidence_calls[-1]) == set(selected)
    assert set(env.identity_calls[-1]) == set(selected)
    part = paper['views']['combined' if view=='split' else view]
    assert part['overall']['receipts']['ledger'] == receipt
    assert part['overall']['payments']['ledger'] == payment
    assert part['overall']['receipts']['difference'] == part['overall']['payments']['difference'] == 0
    assert part['overall_balance_proof']['ledger_closing'] == balance['closing_total']
    drill = env.client.get(base+'/drilldown', params=params).json()
    assert drill['account_selection'] == report['account_selection']
    assert drill['ledger_context'] == report['ledger_context']
    exported = env.client.get(base+'/export', params=params)
    assert exported.status_code == 200, exported.text
    assert 'Cashflows_selected' in exported.headers['content-disposition']
    workbook = load_workbook(BytesIO(exported.content), data_only=True)
    controls = dict(workbook['Export Controls'].values)
    assert {value for key,value in controls.items() if str(key).startswith('Selected Cash/Bank account ')} == set(selected)
    # Existing workbook proof fields must agree with API values, including transfer movement.
    proof = list(workbook['Treasury Movement Proof'].values)
    assert len(proof) > 4
    assert 'selected Cash/Bank' in workbook['Cashflows Summary']['A2'].value


@pytest.mark.parametrize('selection', [[], [''], ['UNKNOWN'], ['C1','UNKNOWN']])
def test_selected_routes_reject_invalid_scope_before_reading_financial_source(selected_report_env, selection):
    env = selected_report_env
    # A blank repeated query item represents an explicit empty selection over HTTP.
    params = env.params | dict(selected_treasury_account_codes=selection or [''])
    for suffix in ['/preview','/drilldown','/export','/differences','/reconciliation','/reconciliation/cache-validation']:
        response = env.client.get('/api/reconciliation/cashflows'+suffix, params=params)
        assert response.status_code == 400, (suffix,response.text)
    assert not env.source_calls and not env.evidence_calls and not env.identity_calls


def test_selected_identity_is_order_independent_separate_from_all_and_sensitive_to_sources(selected_report_env):
    env = selected_report_env; url = '/api/reconciliation/cashflows/reconciliation/cache-validation'
    def get(codes=None):
        params = env.params | (dict(selected_treasury_account_codes=codes) if codes is not None else {})
        response = env.client.get(url, params=params)
        assert response.status_code == 200, response.text
        return response.json()
    all_accounts = get()
    assert get(list(env.case['accounts'])) == all_accounts
    first = get(['C2',' C1 ','C1'])
    assert get(['C1','C2']) == first
    assert len({get(['C1'])['cache_identity'], get(['C2'])['cache_identity'],
        first['cache_identity'], all_accounts['cache_identity']}) == 4
    env.stamp[0] = 'changed-source-without-revision'
    assert get(['C1','C2'])['cache_identity'] != first['cache_identity']


def test_cashflow_account_picker_reads_only_configured_metadata_in_one_snapshot(selected_report_env,monkeypatch):
    env=selected_report_env
    def names(client):
        assert env.active==[True]
        return [dict(account_code='C1',account_name='Cash <main>'),dict(account_code='UNCONFIGURED',account_name='Excluded')]
    monkeypatch.setattr(reconciliation,'_latest_trial_balance_accounts',names)
    url='/api/reconciliation/cashflows/account-options'
    response=env.client.get(url,params={'client_id':7})
    assert response.status_code==200,response.text
    payload=response.json()
    assert payload['client_id']==7 and payload['configuration_key']==reconciliation._cashflow_configuration_key(env.config)
    assert {r['account_code'] for r in payload['accounts']}==set(env.case['accounts'])
    assert next(r for r in payload['accounts'] if r['account_code']=='C1')['account_name']=='Cash <main>'
    assert next(r for r in payload['accounts'] if r['account_code']=='B1')['account_name']=='B1'
    assert all(r['treasury_type']==env.case['accounts'][r['account_code']].lower() for r in payload['accounts'])
    assert not env.source_calls and not env.evidence_calls and not env.identity_calls
    assert env.client.get(url,params={'client_id':0}).status_code==422
    from fastapi import HTTPException
    def reject():raise HTTPException(403,'Forbidden')
    env.client.app.dependency_overrides[reconciliation.require_admin]=reject
    assert env.client.get(url,params={'client_id':7}).status_code==403


def test_selected_preview_and_drilldown_share_configuration_and_source_snapshot_without_changing_all(selected_report_env):
    env=selected_report_env
    for suffix in ['/preview','/drilldown']:
        url='/api/reconciliation/cashflows'+suffix
        assert env.client.get(url,params=env.params).status_code==200
        assert env.source_snapshots[-1] is False
        assert env.client.get(url,params=env.params|dict(selected_treasury_account_codes=['C1'])).status_code==200
        assert env.source_snapshots[-1] is True


def test_explicit_all_api_preserves_payloads_and_omits_selection_keywords(selected_report_env):
    env=selected_report_env
    for view in ['combined','bank','cash','split']:
        params=env.params|dict(treasury_view=view, include_balances=True)
        first=env.client.get('/api/reconciliation/cashflows/preview',params=params).json()
        second=env.client.get('/api/reconciliation/cashflows/preview',params=params|dict(selected_treasury_account_codes=list(env.case['accounts']))).json()
        first['freshness'].pop('generated_at');second['freshness'].pop('generated_at')
        assert first==second
        assert 'account_selection' not in second
    assert all('selected_treasury_account_codes' not in call for call in env.source_calls)


def test_selected_support_cannot_be_opened_with_another_selection_fingerprint(selected_report_env):
    env=selected_report_env; url='/api/reconciliation/cashflows/reconciliation'
    first=env.client.get(url,params=env.params|dict(selected_treasury_account_codes=['C1'])).json()
    rejected=env.client.get(url,params=env.params|dict(selected_treasury_account_codes=['C2'],
        expected_fingerprint=first['evidence_fingerprint']))
    assert rejected.status_code==409
    first_key=first['views']['combined']['adjustments'][0]['key']
    supported=env.client.get(url,params=env.params|dict(selected_treasury_account_codes=['C1'],
        expected_fingerprint=first['evidence_fingerprint'],support_key=first_key))
    assert supported.status_code==200,supported.text
    assert supported.json()['account_selection']['account_codes']==['C1']


def test_selected_api_does_not_require_unselected_ledgers_or_invent_missing_selected_balances(selected_report_env):
    env=selected_report_env; url='/api/reconciliation/cashflows/preview'
    env.case['source']['uploads']=[u for u in env.case['source']['uploads'] if u['ledger_account_code']=='C1']
    params=env.params|dict(include_balances=True)
    selected=env.client.get(url,params=params|dict(selected_treasury_account_codes=['C1'])).json()
    assert selected['preview']['balance_reconciliation']['status']=='Matched'
    missing=env.client.get(url,params=params|dict(selected_treasury_account_codes=['C2'])).json()
    assert missing['source']['ready'] is True
    assert missing['preview']['balance_reconciliation']['status']=='Unavailable'
    assert missing['preview']['balance_reconciliation']['overall']['closing_total'] is None


def test_selected_api_keeps_allocation_uncertainty_visible_even_when_amounts_reconcile(selected_report_env):
    env=selected_report_env
    frame=pd.DataFrame([['AMB','2026-01-05','B1',0,60],['AMB','2026-01-05','C1',0,40],
        ['AMB','2026-01-05','EXP1',100,0]],columns=['voucher_id','date','account_code','debit','credit'])
    env.case['journal']=frame;env.case['source']=env.case['make_source'](frame)
    params=env.params|dict(selected_treasury_account_codes=['C1'],include_balances=True)
    report=env.client.get('/api/reconciliation/cashflows/preview',params=params).json()
    assert report['controls']['selection_allocation']['status']=='Review'
    assert report['preview']['selection_review_count']==1
    assert report['preview']['selection_review_details'][0]['payments']==40
    assert report['preview']['balance_reconciliation']['overall']['closing_total']==9960
    paper=env.client.get('/api/reconciliation/cashflows/reconciliation',params=params).json()
    assert paper['review_controls']['selection_review_count']==1
    assert paper['views']['combined']['status']=='Review required'
    assert paper['transaction_coverage_status']=='Not independently certified'


def test_selected_export_job_normalizes_scope_rejects_cross_scope_and_revalidates_download(selected_report_env,tmp_path,monkeypatch):
    import time
    from services.reconciliation_export_jobs import ReconciliationExportJobs
    env=selected_report_env; url='/api/reconciliation/cashflows/reconciliation'
    manager=ReconciliationExportJobs(tmp_path/'jobs')
    monkeypatch.setattr(reconciliation,'export_jobs',manager)
    def wait(ticket):
        for _ in range(200):
            status=env.client.get(url+'/exports/'+ticket['id'],params={'client_id':7}).json()
            if status['state'] in ('ready','failed','cancelled'): return status
            time.sleep(.02)
        pytest.fail('Selected fixture export did not finish')
    try:
        params=env.params|dict(selected_treasury_account_codes=['C1','C2'])
        parent=env.client.get(url,params=params).json()
        payload=params|dict(expected_identity=parent['cache_identity'],expected_fingerprint=parent['evidence_fingerprint'])
        response=env.client.post(url+'/exports',json=payload)
        assert response.status_code==202,response.text
        ticket=response.json()
        assert wait(ticket)['state']=='ready'
        reordered=env.client.post(url+'/exports',json=payload|dict(selected_treasury_account_codes=[' C2 ','C1','C2']))
        assert reordered.status_code==202 and reordered.json()['id']==ticket['id']
        downloaded=env.client.get(url+'/exports/'+ticket['id']+'/download',params={'client_id':7})
        assert downloaded.status_code==200,downloaded.text
        assert 'Cashflow_reconciliation_selected_' in downloaded.headers['content-disposition']
        workbook=load_workbook(BytesIO(downloaded.content),data_only=True)
        values={r[0]:r[1] for r in workbook['Working Paper Combined'].values}
        assert values['Receipts as per Cashflow report']==parent['views']['combined']['overall']['receipts']['cashflow']
        assert values['Payments as per Cashflow report']==parent['views']['combined']['overall']['payments']['cashflow']
        bad=env.client.post(url+'/exports',json=payload|dict(selected_treasury_account_codes=['C3']))
        assert bad.status_code==202 and wait(bad.json())['error_status']==409
        for codes in ([],['UNKNOWN'],['']):
            assert env.client.post(url+'/exports',json=payload|dict(selected_treasury_account_codes=codes)).status_code==400
        env.stamp[0]='independent-ledger-correction'
        stale=env.client.get(url+'/exports/'+ticket['id']+'/download',params={'client_id':7})
        assert stale.status_code==409
        assert env.client.get(url+'/exports/'+ticket['id'],params={'client_id':7}).json()['state']=='cancelled'
    finally:
        assert manager.stop()


@pytest.fixture
def independent_year_boundary_report(independent_year_boundary_case):
    """Run the real report and balance engines, isolating only external reads."""
    from services.reconciliation_balance_source import build_uploaded_ledger_balances
    case = independent_year_boundary_case
    def report(view='split', presentation='net', account_codes=None):
        accounts = account_codes or case['accounts']
        configuration = dict(source_template_id=1, cash_account_codes=[c for c, t in accounts.items() if t == 'Cash'],
            bank_account_codes=[c for c, t in accounts.items() if t == 'Bank'],
            match_internal_transfers=True, receipt_presentation=presentation,
            customer_payment_roles=case['roles'], vendor_account_codes=['AP'], cashflow_report_revision=7)
        source = SimpleNamespace(journal=case['journal'], coa=case['coa'], ready=True,
            diagnostics=(), metadata={'cashflow_report_source_key': 'independent-books:7:1'})
        active = []
        def snapshot(callback):
            active.append(True)
            try:
                return callback(None)
            finally:
                active.pop()
        def load_source(*args, **kwargs):
            assert active == [True]
            return source
        def balances(client_id, selected, periods, movements, **kwargs):
            assert active == [True]
            return build_uploaded_ledger_balances(case['uploads'], case['rows'], selected, periods, movements,
                account_names=kwargs['account_names'], treasury_movements=kwargs['treasury_movements'])
        with patch.object(reconciliation, '_require_active_client'), \
                patch.object(reconciliation, '_cashflow_configuration', return_value=configuration), \
                patch.object(reconciliation, '_latest_trial_balance_accounts', return_value=[]), \
                patch.object(reconciliation, 'load_ledger_report_source', side_effect=load_source), \
                patch.object(reconciliation, 'load_uploaded_ledger_balances', side_effect=balances), \
                patch.object(reconciliation, 'run_read_snapshot_pg', side_effect=snapshot):
            return reconciliation.preview_cashflows(7, case['start'], case['end'], group_by='month',
                treasury_view=view, details=None, include_balances=True, session=None)
    return report


@pytest.mark.parametrize('presentation', ['net', 'gross'])
@pytest.mark.parametrize('view', ['combined', 'bank', 'cash', 'split'])
def test_independent_report_response_matches_books_and_signed_party_controls(
        independent_year_boundary_report, independent_year_boundary_case, view, presentation):
    response = independent_year_boundary_report(view, presentation)
    preview = response['preview']
    expected = independent_year_boundary_case['expected']['combined' if view == 'split' else view]
    assert [(p['start'], p['end']) for p in preview['periods']] == [
        ('2025-12-15', '2025-12-31'), ('2026-01-01', '2026-01-31'), ('2026-02-01', '2026-02-10')]
    assert [p['cashflow_net_movement'] for p in preview['balance_reconciliation']['periods']] == pytest.approx(
        [float(row[0]) for row in expected])
    assert [p['calculated_closing'] for p in preview['balance_reconciliation']['periods']] == pytest.approx(
        [float(row[2]) for row in expected])
    assert preview['balance_reconciliation']['status'] == 'Matched'
    assert preview['treasury_reconciliation']['status'] == 'Review required'
    assert preview['treasury_reconciliation']['source_status'] == 'Matched'
    # The original settlement and its later partial return retain their own dates.
    customers = {r['voucher_id']: r for r in preview['customer_payment_details']}
    assert set(customers) == {'RECEIPT', 'REVERSAL', 'REPEATS'}
    assert customers['REVERSAL']['period_key'] == '2026-01'
    assert customers['REVERSAL']['bank_charges'] == pytest.approx(-1.10)
    assert customers['REVERSAL']['customer_reversals'] == 40
    assert customers['REPEATS']['net_bank_settlement'] == pytest.approx(6.66)
    assert preview['vendor_payment_vendor_totals'][0]['net_payments'] == pytest.approx(20.40)


def test_configuration_only_account_addition_requires_new_ledger_evidence(independent_year_boundary_report):
    before = independent_year_boundary_report()
    after = independent_year_boundary_report(account_codes={'B1': 'Bank', 'B0': 'Bank', 'C1': 'Cash', 'NEW': 'Cash'})
    assert before['ledger_context']['report_revision'] == after['ledger_context']['report_revision']
    assert before['ledger_context']['configuration_key'] != after['ledger_context']['configuration_key']
    balances = after['preview']['balance_reconciliation']
    assert balances['status'] == 'Unavailable'
    assert balances['overall']['closing_total'] is None
    assert balances['treasury_breakdown']['bank']['status'] == 'Matched'
    assert after['preview']['treasury_movement_proof'] == before['preview']['treasury_movement_proof']


def _source():
    return SimpleNamespace(
        journal=pd.DataFrame(),
        coa=pd.DataFrame(),
        diagnostics=(),
        ready=True,
        metadata={"source_upload_ids": [21], "source_row_count": 4},
    )


def _balance_source(ready=True, diagnostics=()):
    return SimpleNamespace(
        opening_balances={"B1": 1000},
        actual_closing_balances={"B1": {"2026-01": 1100}},
        diagnostics=diagnostics,
        ready=ready,
        months=["2026-01"],
    )


def _summary():
    return {
        "group_label": "Bank",
        "months": ["2026-01"],
        "selected_vouchers": ["V1"],
        "internal_transfers": pd.DataFrame(),
        "categories_needing_review": pd.DataFrame(),
        "category_totals": pd.DataFrame({"2026-01": [100.0], "Total": [100.0]}, index=["Customers"]),
        "monthly_balance_checks": pd.DataFrame(
            [{"month": "2026-01", "category_total": 100.0, "group_total": 100.0, "variance": 0.0, "balanced": True}]
        ),
        "reconciliation_proof": pd.DataFrame(
            [{"month": "2026-01", "opening_balance": 1000.0, "movement": 100.0, "actual_closing_balance": 1100.0, "variance": 0.0, "status": "Matched"}]
        ),
        "reconciliation_available": True,
        "reconciliation_message": None,
    }


def _cashflow_summary():
    return {
        "months": ["2026-02", "2026-03", "2026-04"],
        "periods": [
            {"key": "2026-Q1", "start": "2026-02-10", "end": "2026-03-31", "label": "Q1 2026"},
            {"key": "2026-Q2", "start": "2026-04-01", "end": "2026-04-18", "label": "Q2 2026"},
        ],
        "group_by": "quarter",
        "category_totals": pd.DataFrame({"2026-Q1": [125.0], "2026-Q2": [50.0], "Total": [175.0]}, index=["Customers"]),
        "transfer_details": pd.DataFrame(),
        "customer_payment_details": pd.DataFrame(),
        "deduction_details": pd.DataFrame(),
        "categories_needing_review": pd.DataFrame(),
        "selected_voucher_count": 2,
        "internal_transfer_count": 0,
        "category_review_count": 0,
        "receipt_presentation": "net",
    }


def test_balance_opt_in_keeps_ordinary_preview_off_the_new_snapshot_path():
    with patch.object(reconciliation, '_build_cashflow_result', return_value={'preview': {}}) as builder, \
            patch.object(reconciliation, 'run_read_snapshot_pg') as snapshot:
        reconciliation.preview_cashflows(7, '2026-01-01', '2026-01-31', session=None)
    snapshot.assert_not_called()
    assert 'include_balances' not in builder.call_args.kwargs


def test_balance_opt_in_builds_vouchers_and_ledger_evidence_in_one_read_snapshot():
    active = []
    def snapshot(callback):
        active.append(True)
        try:
            return callback(object())
        finally:
            active.pop()
    def build(*args, **kwargs):
        assert active == [True]
        assert kwargs['include_balances'] is True
        return {'preview': {'balance_reconciliation': {'status': 'Matched'}}}
    with patch.object(reconciliation, '_build_cashflow_result', side_effect=build) as builder, \
            patch.object(reconciliation, 'run_read_snapshot_pg', side_effect=snapshot) as runner:
        result = reconciliation.preview_cashflows(7, '2026-01-01', '2026-01-31',
                                                  include_balances=True, session=None)
    assert result['preview']['balance_reconciliation']['status'] == 'Matched'
    runner.assert_called_once()
    builder.assert_called_once()
    assert active == []


@pytest.mark.parametrize('view,expected_codes,expected_flows', [
    ('combined', {'C1': 'Cash', 'B1': 'Bank'}, [125, 50]),
    ('split', {'C1': 'Cash', 'B1': 'Bank'}, [125, 50]),
    ('bank', {'B1': 'Bank'}, [5, 20]),
    ('cash', {'C1': 'Cash'}, [120, 30]),
])
def test_cashflow_balance_reconciliation_uses_same_report_periods_view_and_category_effect(view, expected_codes, expected_flows):
    from services.ledger_group_summary import CASHFLOW_TREASURY_TOTALS_VERSION
    configuration = dict(source_template_id=9, cash_account_codes=['C1'], bank_account_codes=['B1'],
                         match_internal_transfers=True, receipt_presentation='net')
    summary = _cashflow_summary()
    summary['treasury_totals_version'] = CASHFLOW_TREASURY_TOTALS_VERSION
    summary['treasury_category_totals'] = pd.DataFrame([
        {'category': 'Customers', 'period_key': '2026-Q1', 'bank_amount': 80, 'cash_amount': 45, 'total_amount': 125},
        {'category': 'Customers', 'period_key': '2026-Q2', 'bank_amount': 20, 'cash_amount': 30, 'total_amount': 50},
    ])
    summary['treasury_movement_proof'] = [dict(period_key=key, treasury_type=kind, status='Matched', explained_movement=amount)
        for kind, values in [('bank', [5, 20]), ('cash', [120, 30]), ('combined', [125, 50])]
        for key, amount in zip(['2026-Q1', '2026-Q2'], values)]
    evidence = {'ready': False, 'status': 'Unavailable', 'periods': [], 'evidence_fingerprint': 'current-proof'}
    with patch.object(reconciliation, '_require_active_client'), \
            patch.object(reconciliation, '_cashflow_configuration', return_value=configuration), \
            patch.object(reconciliation, '_latest_trial_balance_accounts', return_value=[{'account_code': 'B1', 'account_name': 'Bank Alpha'}]), \
            patch.object(reconciliation, 'load_ledger_report_source', return_value=_source()), \
            patch.object(reconciliation, 'build_cashflow_summary', return_value=summary), \
            patch.object(reconciliation, 'load_uploaded_ledger_balances', return_value=evidence) as balances:
        result = reconciliation._build_cashflow_result(7, '2026-02-10', '2026-04-18',
                                                       group_by='quarter', treasury_view=view, include_balances=True)
    args, kwargs = balances.call_args
    assert args[:3] == (7, expected_codes, summary['periods'])
    assert [float(v) for v in args[3].values()] == expected_flows
    assert kwargs['query_fn'] is reconciliation.execute_query_pg
    assert kwargs['account_names'] == ({'B1': 'Bank Alpha'} if view != 'cash' else {})
    assert (kwargs['treasury_movements'] is not None) == (view == 'split')
    if view == 'split':
        assert [float(v) for v in kwargs['treasury_movements']['bank'].values()] == [5, 20]
        assert [float(v) for v in kwargs['treasury_movements']['cash'].values()] == [120, 30]
        assert result['preview']['balance_reconciliation']['unresolved_movements'] == {'2026-Q1': 0, '2026-Q2': 0}
    assert result['preview']['balance_reconciliation'] == evidence
    # Missing ledger evidence must not erase or block the working movement report.
    assert result['preview']['category_totals'][0]['2026-Q1'] == 125
    assert result['blocking_reasons'] == []
    assert result['source']['ready'] is True


def test_balance_opt_in_public_http_parameter_and_admin_guard():
    from fastapi import FastAPI, HTTPException
    from fastapi.testclient import TestClient
    app = FastAPI()
    app.include_router(reconciliation.router)
    app.dependency_overrides[reconciliation.require_admin] = lambda: {'id': 1}
    with TestClient(app) as client, \
            patch.object(reconciliation, '_build_cashflow_result', return_value={'preview': {}}) as builder, \
            patch.object(reconciliation, 'run_read_snapshot_pg', side_effect=lambda callback: callback(None)) as snapshot:
        response = client.get('/api/reconciliation/cashflows/preview', params={
            'client_id': 7, 'period_start': '2026-01-01', 'period_end': '2026-01-31',
            'details': 'summary', 'include_balances': 'true'})
        assert response.status_code == 200
        snapshot.assert_called_once()
        assert builder.call_args.kwargs['include_balances'] is True
        def reject():
            raise HTTPException(401, 'Login required')
        app.dependency_overrides[reconciliation.require_admin] = reject
        builder.reset_mock()
        response = client.get('/api/reconciliation/cashflows/preview', params={
            'client_id': 7, 'period_start': '2026-01-01', 'period_end': '2026-01-31', 'include_balances': 'true'})
        assert response.status_code == 401
        builder.assert_not_called()


def test_balance_snapshot_failure_cannot_return_a_previous_successful_response():
    with patch.object(reconciliation, 'run_read_snapshot_pg', side_effect=RuntimeError('snapshot failed')), \
            patch.object(reconciliation, '_build_cashflow_result') as builder, pytest.raises(RuntimeError, match='snapshot failed'):
        reconciliation.preview_cashflows(7, '2026-01-01', '2026-01-31', include_balances=True, session=None)
    builder.assert_not_called()


def test_difference_endpoint_is_isolated_authenticated_and_paginates_whole_groups():
    from fastapi import FastAPI, HTTPException
    from fastapi.testclient import TestClient
    app=FastAPI(); app.include_router(reconciliation.router)
    app.dependency_overrides[reconciliation.require_admin]=lambda: {'id':1}
    config=dict(source_template_id=9,bank_account_codes=['B'],cash_account_codes=['C'],cashflow_report_revision=7)
    active=[]
    def snapshot(callback):
        active.append(True)
        try: return callback(None)
        finally: active.pop()
    def source(*args,**kwargs):
        assert active==[True]
        assert args[:3]==(7,9,['B'])
        return {}
    # Supporting lines belong to a group and must not be silently capped at 200.
    rows=[dict(status='matched',voucher_id='M'),dict(status='changed',voucher_id='A',voucher_lines=[{'id':i} for i in range(251)]),dict(status='ledger_only',voucher_id='B')]
    with TestClient(app) as client, patch.object(reconciliation,'_require_active_client'), \
            patch.object(reconciliation,'_cashflow_configuration',return_value=config), \
            patch.object(reconciliation,'run_read_snapshot_pg',side_effect=snapshot), \
            patch.object(reconciliation,'load_cashflow_difference_source',side_effect=source) as loader, \
            patch.object(reconciliation,'build_cashflow_differences',side_effect=lambda *a: {'findings':copy.deepcopy(rows)}) , \
            patch.object(reconciliation,'_build_cashflow_result') as summary:
        params=dict(client_id=7,period_start='2026-01-01',period_end='2026-01-31',treasury_view='bank',limit=1)
        response=client.get('/api/reconciliation/cashflows/differences',params=params)
        assert response.status_code==200
        result=response.json()
        assert result['total_groups']==3 and result['total_rows']==2 and result['page_count']==2
        assert len(result['rows'][0]['voucher_lines'])==251
        assert client.get('/api/reconciliation/cashflows/differences',params=params|{'page':2}).json()['rows'][0]['voucher_id']=='B'
        assert client.get('/api/reconciliation/cashflows/differences',params=params|{'differences_only':False}).json()['total_rows']==3
        for change in ({'treasury_view':'wrong'},{'period_start':'bad'},{'limit':201},{'page':0}):
            assert client.get('/api/reconciliation/cashflows/differences',params=params|change).status_code in [400,422]
        summary.assert_not_called()
        def reject(): raise HTTPException(401,'Login required')
        app.dependency_overrides[reconciliation.require_admin]=reject
        loader.reset_mock()
        assert client.get('/api/reconciliation/cashflows/differences',params=params).status_code==401
        loader.assert_not_called()


def test_difference_snapshot_failure_does_not_return_old_diagnostics():
    with patch.object(reconciliation,'run_read_snapshot_pg',side_effect=RuntimeError('read failed')), \
            patch.object(reconciliation,'load_cashflow_difference_source') as loader, pytest.raises(RuntimeError,match='read failed'):
        reconciliation.cashflow_differences(7,'2026-01-01','2026-01-31',treasury_view='combined',session=None)
    loader.assert_not_called()


def test_horizontal_endpoint_uses_one_snapshot_and_returns_paginated_current_support(horizontal_reconciliation_case):
    from fastapi import FastAPI, HTTPException
    from fastapi.testclient import TestClient
    case=horizontal_reconciliation_case
    config=dict(source_template_id=1,bank_account_codes=['B1'],cash_account_codes=['C1'],cashflow_report_revision=7,
        receipt_presentation='gross',match_internal_transfers=True,customer_payment_roles=case['roles'])
    report=SimpleNamespace(journal=case['journal'],coa=case['coa'],ready=True)
    active=[]
    def snapshot(callback):
        active.append(True)
        try:return callback(None)
        finally:active.pop()
    def load_report(*args,**kwargs):
        assert active==[True]
        return report
    def load_evidence(*args,**kwargs):
        assert active==[True]
        return case['source']
    def load_identity(*args,**kwargs):
        assert active==[True]
        return 'a'*64
    app=FastAPI();app.include_router(reconciliation.router)
    app.dependency_overrides[reconciliation.require_admin]=lambda:{'id':1}
    with TestClient(app) as client, patch.object(reconciliation,'_require_active_client'), \
            patch.object(reconciliation,'_cashflow_configuration',return_value=config), \
            patch.object(reconciliation,'run_read_snapshot_pg',side_effect=snapshot), \
            patch.object(reconciliation,'load_ledger_report_source',side_effect=load_report), \
            patch.object(reconciliation,'load_cashflow_difference_source',side_effect=load_evidence), \
            patch.object(reconciliation,'load_cashflow_reconciliation_identity',side_effect=load_identity), \
            patch.object(reconciliation,'_build_cashflow_result') as ordinary:
        params=dict(client_id=7,period_start='2026-01-01',period_end='2026-01-31')
        url='/api/reconciliation/cashflows/reconciliation'
        response=client.get(url,params=params)
        assert response.status_code==200,response.text
        result=response.json()
        validation=client.get(url+'/cache-validation',params=params)
        assert validation.status_code==200,validation.text
        assert validation.json()['cache_identity']==result['cache_identity']
        assert validation.json()['cache_schema']==1
        assert '_support_entries' not in result and 'support' not in result
        assert result['views']['combined']['amount_status']=='Matched'
        counts=result['views']['combined']['voucher_completeness']
        assert counts['ledger']['vouchers']==counts['transaction']['vouchers']==4
        assert counts['cashflow']['vouchers']==3 and counts['source_ids_match'] is True
        adjustment=next(row for row in result['views']['combined']['adjustments'] if row['category']=='Tax')
        support_params=params|dict(support_key=adjustment['key'],expected_fingerprint=result['evidence_fingerprint'],limit=1)
        child=client.get(url,params=support_params)
        assert child.status_code==200,child.text
        assert 'voucher_completeness' not in child.json()['views']['combined']
        support=child.json()['support']
        assert support['rows'][0]['voucher_id']=='RECEIPT'
        assert len(support['rows'][0]['voucher_lines'])==4
        assert support['rows'][0]['receipts']==20
        assert client.get(url,params=support_params|{'expected_fingerprint':'stale'}).status_code==409
        # Posting pages use the parent fingerprint even when the selected proof is Bank/Cash.
        for view in ('combined','bank','cash','split'):
            parent=client.get(url,params=params|{'treasury_view':view}).json()
            part='bank' if view=='split' else view
            posting_params=params|dict(treasury_view=view,posting_differences=True,comparison_view=part,
                expected_fingerprint=parent['evidence_fingerprint'],differences_only=False,limit=1)
            posting=client.get(url,params=posting_params)
            assert posting.status_code==200,posting.text
            data=posting.json()['posting_differences']
            assert len(data['rows'])==1 and data['rows'][0]['status']=='matched'
            assert data['total_rows']==(5 if view=='combined' else 1 if view=='cash' else 4)
            assert posting.json()['evidence_fingerprint']==parent['evidence_fingerprint']
            assert len(data['rows'][0]['voucher_lines'])>=len(data['rows'][0]['transaction_lines'])
            second=client.get(url,params=posting_params|{'page':2}).json()['posting_differences']
            assert second['page']==min(2,second['page_count'])
            assert client.get(url,params=posting_params|{'expected_fingerprint':'old'}).status_code==409
            assert client.get(url,params=posting_params|{'comparison_view':'unknown'}).status_code==400
            assert client.get(url,params=posting_params|{'period_key':'2026-01'}).status_code==400
            assert client.get(url,params=posting_params|{'support_key':adjustment['key']}).status_code==400
            empty=client.get(url,params=posting_params|{'differences_only':True}).json()['posting_differences']
            assert empty['total_rows']==0 and empty['rows']==[] and empty['counts']['matched']>0
        for change in ({'treasury_view':'wrong'},{'group_by':'wrong'},{'period_start':'bad'},
                       {'page':0},{'limit':201},{'support_key':'unknown'},{'period_key':'wrong'}):
            assert client.get(url,params=params|change).status_code in (400,422)
        ordinary.assert_not_called()
        def reject():raise HTTPException(401,'Login required')
        app.dependency_overrides[reconciliation.require_admin]=reject
        assert client.get(url,params=params).status_code==401


def test_reconciliation_cache_validation_is_authenticated_snapshot_only_and_source_sensitive():
    from fastapi import FastAPI,HTTPException
    from fastapi.testclient import TestClient
    config=dict(source_template_id=1,bank_account_codes=['B1'],cash_account_codes=['C1'],cashflow_report_revision=7,
        receipt_presentation='gross',match_internal_transfers=True,customer_payment_roles={},category_mappings={})
    active=[];stamp=['a'*64]
    def snapshot(callback):
        active.append(True)
        try:return callback(None)
        finally:active.pop()
    def identity(*args,**kwargs):
        assert active==[True]
        assert args==(7,1,['B1','C1'])
        return stamp[0]
    app=FastAPI();app.include_router(reconciliation.router)
    app.dependency_overrides[reconciliation.require_admin]=lambda:{'id':1}
    with TestClient(app) as client, patch.object(reconciliation,'_require_active_client'), \
            patch.object(reconciliation,'_cashflow_configuration',return_value=config), \
            patch.object(reconciliation,'run_read_snapshot_pg',side_effect=snapshot), \
            patch.object(reconciliation,'load_cashflow_reconciliation_identity',side_effect=identity), \
            patch.object(reconciliation,'build_cashflow_summary') as build, \
            patch.object(reconciliation,'load_cashflow_difference_source') as evidence:
        url='/api/reconciliation/cashflows/reconciliation/cache-validation'
        params=dict(client_id=7,period_start='2026-01-01',period_end='2026-01-31')
        def get(change=None):
            response=client.get(url,params=params|(change or {}))
            assert response.status_code==200,response.text
            return response.json()
        first=get();assert first==get()
        assert set(first)=={'cache_schema','cache_identity','client_id','template_id','period_start','period_end',
            'group_by','treasury_view','configuration_key','report_revision'}
        stamp[0]='b'*64  # Independent ledger/source edit without a Cashflow revision increment.
        assert get()['cache_identity']!=first['cache_identity']
        second=get();config['category_mappings']={'100':'New category'}
        assert get()['cache_identity']!=second['cache_identity']
        current=get()
        for change in ({'period_end':'2026-02-28'},{'group_by':'quarter'},{'treasury_view':'split'}):
            assert get(change)['cache_identity']!=current['cache_identity']
        for change in ({'client_id':0},{'period_start':'bad'},{'group_by':'bad'},{'treasury_view':'bad'}):
            assert client.get(url,params=params|change).status_code in (400,422)
        build.assert_not_called();evidence.assert_not_called()
        def reject():raise HTTPException(403,'Forbidden')
        app.dependency_overrides[reconciliation.require_admin]=reject
        assert client.get(url,params=params).status_code==403
    with patch.object(reconciliation,'run_read_snapshot_pg',side_effect=RuntimeError('read failed')), \
            pytest.raises(RuntimeError,match='read failed'):
        reconciliation.validate_cashflow_reconciliation_cache(7,'2026-01-01','2026-01-31',
            group_by='month',treasury_view='combined',session=None)


def test_horizontal_endpoint_never_reuses_old_results_after_snapshot_failure():
    with patch.object(reconciliation,'run_read_snapshot_pg',side_effect=RuntimeError('read failed')), \
            patch.object(reconciliation,'load_ledger_report_source') as loader,pytest.raises(RuntimeError,match='read failed'):
        reconciliation.cashflow_reconciliation(7,'2026-01-01','2026-01-31',group_by='month',treasury_view='combined',session=None)
    loader.assert_not_called()


@pytest.mark.parametrize('view', ['combined', 'bank', 'cash', 'split'])
def test_reconciliation_excel_jobs_use_one_build_and_fresh_authenticated_download(horizontal_reconciliation_case, view, tmp_path):
    import time
    from fastapi import FastAPI, HTTPException
    from fastapi.testclient import TestClient
    from services.reconciliation_export_jobs import ReconciliationExportJobs
    case = horizontal_reconciliation_case
    config = dict(source_template_id=1, bank_account_codes=['B1'], cash_account_codes=['C1'],
        cashflow_report_revision=7, receipt_presentation='gross', match_internal_transfers=True,
        customer_payment_roles=case['roles'])
    active, snapshots = [], []
    manager = ReconciliationExportJobs(tmp_path/'jobs')
    def snapshot(callback):
        assert not active
        active.append(True); snapshots.append(1)
        try:
            return callback(None)
        finally:
            active.pop()
    def source(*args, **kwargs):
        assert active == [True]
        return case['source']
    def report(*args, **kwargs):
        assert active == [True]
        return SimpleNamespace(journal=case['journal'], coa=case['coa'], ready=True)
    def wait(client, ticket):
        for _ in range(200):
            response = client.get(url+'/exports/'+ticket['id'], params={'client_id':7})
            assert response.status_code == 200, response.text
            status = response.json()
            if status['state'] in ('ready','failed','cancelled'):
                return status
            time.sleep(.02)
        pytest.fail('Fixture export did not finish')
    app = FastAPI(); app.include_router(reconciliation.router)
    actor = {'user_id': 1}
    app.dependency_overrides[reconciliation.require_admin] = lambda: actor
    with TestClient(app) as client, patch.object(reconciliation, '_require_active_client'), \
            patch.object(reconciliation, '_cashflow_configuration', return_value=config), \
            patch.object(reconciliation, 'run_read_snapshot_pg', side_effect=snapshot), \
            patch.object(reconciliation, 'load_ledger_report_source', side_effect=report), \
            patch.object(reconciliation, 'load_cashflow_difference_source', side_effect=source), \
            patch.object(reconciliation, 'load_cashflow_reconciliation_identity', return_value='a'*64), \
            patch.object(reconciliation, 'export_jobs', manager), \
            patch.object(reconciliation, '_build_cashflow_result') as ordinary:
        try:
            url = '/api/reconciliation/cashflows/reconciliation'
            params = dict(client_id=7, period_start='2026-01-01', period_end='2026-01-31', treasury_view=view)
            parent = client.get(url, params=params).json()
            assert '_posting_comparisons' not in parent
            params.update(expected_fingerprint=parent['evidence_fingerprint'], expected_identity=parent['cache_identity'])
            count = len(snapshots)
            start = client.post(url+'/exports', json=params)
            assert start.status_code == 202, start.text
            ticket = start.json(); status = wait(client, ticket)
            assert status['state'] == 'ready', status
            assert len(snapshots) == count+1  # one preparation snapshot
            repeat = client.post(url+'/exports', json=params).json()
            assert repeat['id'] == ticket['id'] and len(snapshots) == count+1
            path = url+'/exports/'+ticket['id']
            response = client.get(path+'/download', params={'client_id':7})
            assert response.status_code == 200, response.text
            assert len(snapshots) == count+2  # independent freshness check at download
            assert response.headers['cache-control'] == 'no-store'
            assert f'_{view}_2026-01-01_to_2026-01-31.xlsx' in response.headers['content-disposition']
            workbook = load_workbook(BytesIO(response.content))
            for kind, part in parent['views'].items():
                rows = {r[0].value:r[1].value for r in workbook['Working Paper '+kind.title()]}
                assert rows['Receipts as per Cashflow report'] == part['overall']['receipts']['cashflow']
                assert rows['Payments as per Cashflow report'] == part['overall']['payments']['cashflow']
            actor['user_id'] = 2
            for suffix in ('','/download'):
                assert client.get(path+suffix, params={'client_id':7}).status_code == 404
            assert client.delete(path, params={'client_id':7}).status_code == 404
            actor['user_id'] = 1
            assert client.get(path, params={'client_id':8}).status_code == 404
            for change in [dict(expected_identity='b'*64),dict(expected_fingerprint='b'*64),dict(period_end='2026-02-28')]:
                failed = client.post(url+'/exports',json=params|change)
                assert failed.status_code == 202, failed.text
                assert wait(client, failed.json())['error_status'] == 409
            for missing in ['expected_identity','expected_fingerprint']:
                assert client.post(url+'/exports',json={k:v for k,v in params.items() if k!=missing}).status_code == 422
            for change in [dict(group_by='wrong'),dict(treasury_view='wrong'),dict(period_start='bad')]:
                assert client.post(url+'/exports',json=params|change).status_code == 400
            # A ledger edit without revision advancement invalidates even a ready file.
            case['source']['ledger_rows'][0]['description'] = 'Corrected source'
            with patch.object(reconciliation,'load_cashflow_reconciliation_identity',return_value='changed'):
                assert client.get(path+'/download',params={'client_id':7}).status_code == 409
            assert client.get(path,params={'client_id':7}).json()['state'] == 'cancelled'
            def reject():
                raise HTTPException(403,'Access denied')
            app.dependency_overrides[reconciliation.require_admin] = reject
            assert client.post(url+'/exports',json=params).status_code == 403
            assert client.get(path,params={'client_id':7}).status_code == 403
            ordinary.assert_not_called()
        finally:
            assert manager.stop()


def test_prepared_rollup_summary_maps_fortnights_across_months():
    def query(sql, params=None):
        if "cashflow_preparation_state" in sql: return [{"active_preparation_revision": 17, "status": "ready"}]
        if "COUNT(DISTINCT voucher_id)" in sql: return [{"period_key": key, "selected_voucher_count": 1} for key in ("2026-01-F1", "2026-01-F2", "2026-02-F1", "2026-02-F2")]
        assert "FROM cashflow_prepared_rollups" in sql
        return [{"period_key": key, "category_label_snapshot": "Sales", "account_code": "4000", "account_name": "Sales", "treasury_type": "", "debit_total": 0, "credit_total": amount, "net_total": -amount, "line_count": 1} for key, amount in (("2026-01-F1", 100), ("2026-01-F2", 200), ("2026-02-F1", 300), ("2026-02-F2", 400))]
    result = load_cashflow_prepared_rollup_summary(
        7, journal_template_id=9, period_start="2026-01-01", period_end="2026-02-28",
        group_by="fortnight", receipt_presentation="net", query_fn=query,
    )
    assert [period["key"] for period in result["summary"]["periods"]] == ["2026-01-1", "2026-01-2", "2026-02-1", "2026-02-2"]
    totals = result["summary"]["category_totals"].loc["Sales"]
    assert [totals[key] for key in ("2026-01-1", "2026-01-2", "2026-02-1", "2026-02-2")] == [100.0, 200.0, 300.0, 400.0]
    assert result["summary"]["selected_voucher_count"] == 4


def _saved_codes(_client_id, group_label=None):
    return {"B1"} if not group_label or group_label.casefold() == "bank" else set()


def test_cashflow_transfer_details_identify_legs_and_preserve_category_totals():
    journal = pd.DataFrame(
        [
            {"voucher_id": "T1", "date": "2026-01-05", "account_code": "BA", "account_name": "Bank Alpha", "voucher_detail": "Bank transfer", "debit": 0, "credit": 100},
            {"voucher_id": "T1", "date": "2026-01-05", "account_code": "BB", "account_name": "Bank Beta", "voucher_detail": "Bank transfer", "debit": 100, "credit": 0},
            {"voucher_id": "T2", "date": "2026-01-06", "account_code": "BA", "account_name": "Bank Alpha", "voucher_detail": "Transfer with fee", "debit": 0, "credit": 100},
            {"voucher_id": "T2", "date": "2026-01-06", "account_code": "BB", "account_name": "Bank Beta", "voucher_detail": "Transfer with fee", "debit": 95, "credit": 0},
            {"voucher_id": "T2", "date": "2026-01-06", "account_code": "CH", "account_name": "Bank charges", "voucher_detail": "Transfer with fee", "debit": 5, "credit": 0},
            {"voucher_id": "T3", "date": "2026-01-07", "account_code": "BA", "account_name": "Bank Alpha", "voucher_detail": "Split transfer", "debit": 0, "credit": 100},
            {"voucher_id": "T3", "date": "2026-01-07", "account_code": "BB", "account_name": "Bank Beta", "voucher_detail": "Split transfer", "debit": 60, "credit": 0},
            {"voucher_id": "T3", "date": "2026-01-07", "account_code": "BC", "account_name": "Bank Central", "voucher_detail": "Split transfer", "debit": 40, "credit": 0},
        ]
    )
    coa = pd.DataFrame(
        [
            {"account_code": "BA", "account_title": "Bank Alpha", "account_type": "Bank", "category": ""},
            {"account_code": "BB", "account_title": "Bank Beta", "account_type": "Bank", "category": ""},
            {"account_code": "BC", "account_title": "Bank Central", "account_type": "Bank", "category": ""},
            {"account_code": "CH", "account_title": "Bank charges", "account_type": "Expense", "category": "Bank charges"},
        ]
    )

    result = build_cashflow_summary(
        journal,
        coa,
        cash_account_codes=[],
        bank_account_codes=["BA", "BB", "BC"],
        period_start="2026-01-01",
        period_end="2026-01-31",
        match_internal_transfers=True,
    )

    details = result["transfer_details"].set_index("voucher_id")
    assert details.loc["T1", "classification"] == "confirmed"
    assert details.loc["T1", "transferor_account_code"] == "BA"
    assert details.loc["T1", "transferor_account_name"] == "Bank Alpha"
    assert details.loc["T1", "transferee_account_code"] == "BB"
    assert details.loc["T1", "transferee_account_name"] == "Bank Beta"
    assert details.loc["T1", "amount"] == 100
    assert details.loc["T1", "variance"] == 0
    assert bool(details.loc["T1", "matched"]) is True
    assert details.loc["T2", "classification"] == "mixed"
    assert details.loc["T2", "status"] == "Review required - mixed voucher"
    assert details.loc["T2", "variance"] == -5
    assert details.loc["T3", "classification"] == "ambiguous"
    assert details.loc["T3", "transferee_account_code"] == "BB / BC"
    assert details.loc["T3", "status"] == "Review required - multi-leg or unbalanced"
    assert result["category_totals"].loc["Bank charges", "2026-01"] == -5
    assert result["transfer_reconciliation"] == {
        "candidate_voucher_count": 3,
        "confirmed_count": 1,
        "mixed_count": 1,
        "ambiguous_count": 1,
        "transferor_total": 300.0,
        "transferee_total": 295.0,
        "variance_total": -5.0,
        "status": "Review required",
    }


def test_cashflow_transfer_reconciliation_confirms_cash_and_bank_routes():
    journal = pd.DataFrame(
        [
            {"voucher_id": "BANK-BANK", "date": "2026-01-01", "account_code": "BA", "account_name": "Bank Alpha", "voucher_detail": "Transfer", "debit": 0, "credit": 50},
            {"voucher_id": "BANK-BANK", "date": "2026-01-01", "account_code": "BB", "account_name": "Bank Beta", "voucher_detail": "Transfer", "debit": 50, "credit": 0},
            {"voucher_id": "BANK-CASH", "date": "2026-01-02", "account_code": "BA", "account_name": "Bank Alpha", "voucher_detail": "Transfer", "debit": 0, "credit": 75},
            {"voucher_id": "BANK-CASH", "date": "2026-01-02", "account_code": "CA", "account_name": "Cash Alpha", "voucher_detail": "Transfer", "debit": 75, "credit": 0},
            {"voucher_id": "CASH-BANK", "date": "2026-01-03", "account_code": "CA", "account_name": "Cash Alpha", "voucher_detail": "Transfer", "debit": 0, "credit": 25},
            {"voucher_id": "CASH-BANK", "date": "2026-01-03", "account_code": "BB", "account_name": "Bank Beta", "voucher_detail": "Transfer", "debit": 25, "credit": 0},
        ]
    )
    coa = pd.DataFrame(
        [
            {"account_code": "BA", "account_title": "Bank Alpha", "account_type": "Bank", "category": ""},
            {"account_code": "BB", "account_title": "Bank Beta", "account_type": "Bank", "category": ""},
            {"account_code": "CA", "account_title": "Cash Alpha", "account_type": "Cash", "category": ""},
        ]
    )

    result = build_cashflow_summary(
        journal,
        coa,
        cash_account_codes=["CA"],
        bank_account_codes=["BA", "BB"],
        period_start="2026-01-01",
        period_end="2026-01-31",
        match_internal_transfers=True,
    )

    assert result["transfer_reconciliation"]["status"] == "Matched"
    assert result["transfer_reconciliation"]["candidate_voucher_count"] == 3
    assert result["transfer_reconciliation"]["confirmed_count"] == 3
    assert result["transfer_reconciliation"]["variance_total"] == 0.0


def test_cashflow_customer_payment_details_reconcile_gross_and_net_methods():
    journal = pd.DataFrame(
        [
            {"voucher_id": "GROSS", "date": "2026-01-01", "account_code": "BANK", "account_name": "Export Bank", "voucher_detail": "Gross receipt", "debit": 100, "credit": 3},
            {"voucher_id": "GROSS", "date": "2026-01-01", "account_code": "CHG", "account_name": "Bank charges", "voucher_detail": "Gross receipt", "debit": 1, "credit": 0},
            {"voucher_id": "GROSS", "date": "2026-01-01", "account_code": "WHT", "account_name": "Withholding tax", "voucher_detail": "Gross receipt", "debit": 2, "credit": 0},
            {"voucher_id": "GROSS", "date": "2026-01-01", "account_code": "CUST-G", "account_name": "Customer Gross", "voucher_detail": "Gross receipt", "debit": 0, "credit": 100},
            {"voucher_id": "NET", "date": "2026-01-02", "account_code": "BANK", "account_name": "Export Bank", "voucher_detail": "Net receipt", "debit": 97, "credit": 0},
            {"voucher_id": "NET", "date": "2026-01-02", "account_code": "CHG", "account_name": "Bank charges", "voucher_detail": "Net receipt", "debit": 1, "credit": 0},
            {"voucher_id": "NET", "date": "2026-01-02", "account_code": "WHT", "account_name": "Withholding tax", "voucher_detail": "Net receipt", "debit": 2, "credit": 0},
            {"voucher_id": "NET", "date": "2026-01-02", "account_code": "CUST-N", "account_name": "Customer Net", "voucher_detail": "Net receipt", "debit": 0, "credit": 100},
        ]
    )
    coa = pd.DataFrame(
        [
            {"account_code": "BANK", "account_title": "Export Bank", "account_type": "Bank", "category": ""},
            {"account_code": "CHG", "account_title": "Bank charges", "account_type": "Expense", "category": "Bank charges"},
            {"account_code": "WHT", "account_title": "Withholding tax", "account_type": "Tax", "category": "Withholding tax"},
            {"account_code": "CUST-G", "account_title": "Customer Gross", "account_type": "Customer", "category": "Received from customers"},
            {"account_code": "CUST-N", "account_title": "Customer Net", "account_type": "Customer", "category": "Received from customers"},
        ]
    )

    result = build_cashflow_summary(
        journal,
        coa,
        cash_account_codes=[],
        bank_account_codes=["BANK"],
        period_start="2026-01-01",
        period_end="2026-01-31",
        receipt_presentation="gross",
    )

    details = result["customer_payment_details"].set_index("voucher_id")
    assert details.loc["GROSS", "method"] == "Gross"
    assert details.loc["GROSS", "bank_statement_amount"] == 100
    assert details.loc["GROSS", "technical_bank_offset"] == 3
    assert details.loc["GROSS", "net_bank_settlement"] == 97
    assert details.loc["GROSS", "expected_net_settlement"] == 97
    assert details.loc["GROSS", "status"] == "Matched"
    assert details.loc["NET", "method"] == "Net"
    assert details.loc["NET", "technical_bank_offset"] == 0
    assert details.loc["NET", "status"] == "Matched"


def test_cashflow_customer_category_identifies_cash_and_bank_child_accounts():
    journal = pd.DataFrame(
        [
            {"voucher_id": "CASH-RECEIPT", "date": "2026-01-01", "account_code": "CASH", "account_name": "Cash in hand", "voucher_detail": "Cash receipt", "debit": 50, "credit": 0},
            {"voucher_id": "CASH-RECEIPT", "date": "2026-01-01", "account_code": "10-15-55-10-10-0001", "account_name": "Export customer one", "voucher_detail": "Cash receipt", "debit": 0, "credit": 50},
            {"voucher_id": "BANK-RECEIPT", "date": "2026-01-02", "account_code": "BANK", "account_name": "Export Bank", "voucher_detail": "Bank receipt", "debit": 75, "credit": 0},
            {"voucher_id": "BANK-RECEIPT", "date": "2026-01-02", "account_code": "10-15-55-10-10-0002", "account_name": "Export customer two", "voucher_detail": "Bank receipt", "debit": 0, "credit": 75},
        ]
    )
    coa = pd.DataFrame(
        [
            {"account_code": "CASH", "account_title": "Cash in hand", "account_type": "Cash", "category": ""},
            {"account_code": "BANK", "account_title": "Export Bank", "account_type": "Bank", "category": ""},
            {"account_code": "10-15-55-10-10-0001", "account_title": "Export customer one", "account_type": "Customer", "category": " Received from customers "},
            {"account_code": "10-15-55-10-10-0002", "account_title": "Export customer two", "account_type": "Customer", "category": "RECEIVED FROM CUSTOMERS"},
        ]
    )

    result = build_cashflow_summary(
        journal,
        coa,
        cash_account_codes=["CASH"],
        bank_account_codes=["BANK"],
        period_start="2026-01-01",
        period_end="2026-01-31",
    )

    details = result["customer_payment_details"].set_index("voucher_id")
    assert set(details.index) == {"CASH-RECEIPT", "BANK-RECEIPT"}
    assert details.loc["CASH-RECEIPT", "bank_account_type"] == "Cash"
    assert details.loc["BANK-RECEIPT", "bank_account_type"] == "Bank"
    assert details.loc["CASH-RECEIPT", "customer_account_code"] == "10-15-55-10-10-0001"
    assert details.loc["BANK-RECEIPT", "customer_account_code"] == "10-15-55-10-10-0002"
    assert details["status"].eq("Matched").all()


def test_cashflow_customer_payment_details_flags_gross_mismatch_and_multi_customer():
    journal = pd.DataFrame(
        [
            {"voucher_id": "MISMATCH", "date": "2026-01-01", "account_code": "BANK", "account_name": "Export Bank", "voucher_detail": "Mismatch", "debit": 100, "credit": 4},
            {"voucher_id": "MISMATCH", "date": "2026-01-01", "account_code": "CHG", "account_name": "Bank charges", "voucher_detail": "Mismatch", "debit": 1, "credit": 0},
            {"voucher_id": "MISMATCH", "date": "2026-01-01", "account_code": "WHT", "account_name": "Withholding tax", "voucher_detail": "Mismatch", "debit": 2, "credit": 0},
            {"voucher_id": "MISMATCH", "date": "2026-01-01", "account_code": "OTHER", "account_name": "Unexplained debit", "voucher_detail": "Mismatch", "debit": 1, "credit": 0},
            {"voucher_id": "MISMATCH", "date": "2026-01-01", "account_code": "CUST", "account_name": "Customer", "voucher_detail": "Mismatch", "debit": 0, "credit": 100},
            {"voucher_id": "MULTI", "date": "2026-01-02", "account_code": "BANK", "account_name": "Export Bank", "voucher_detail": "Multiple customers", "debit": 100, "credit": 0},
            {"voucher_id": "MULTI", "date": "2026-01-02", "account_code": "CUST-1", "account_name": "Customer One", "voucher_detail": "Multiple customers", "debit": 0, "credit": 60},
            {"voucher_id": "MULTI", "date": "2026-01-02", "account_code": "CUST-2", "account_name": "Customer Two", "voucher_detail": "Multiple customers", "debit": 0, "credit": 40},
        ]
    )
    coa = pd.DataFrame(
        [
            {"account_code": "BANK", "account_title": "Export Bank", "account_type": "Bank", "category": ""},
            {"account_code": "CHG", "account_title": "Bank charges", "account_type": "Expense", "category": "Bank charges"},
            {"account_code": "WHT", "account_title": "Withholding tax", "account_type": "Tax", "category": "Withholding tax"},
            {"account_code": "OTHER", "account_title": "Unexplained debit", "account_type": "Expense", "category": "Other expense"},
            {"account_code": "CUST", "account_title": "Customer", "account_type": "Customer", "category": "Received from customers"},
            {"account_code": "CUST-1", "account_title": "Customer One", "account_type": "Customer", "category": "Received from customers"},
            {"account_code": "CUST-2", "account_title": "Customer Two", "account_type": "Customer", "category": "Received from customers"},
        ]
    )

    result = build_cashflow_summary(
        journal,
        coa,
        cash_account_codes=[],
        bank_account_codes=["BANK"],
        period_start="2026-01-01",
        period_end="2026-01-31",
    )

    details = result["customer_payment_details"].set_index("voucher_id")
    assert details.loc["MISMATCH", "method"] == "Gross"
    assert details.loc["MISMATCH", "variance"] == -1
    assert details.loc["MISMATCH", "status"] == "Review required"
    assert details.loc["MULTI", "customer_account_name"] == "Customer One / Customer Two"
    assert details.loc["MULTI", "status"] == "Review required"
def test_cashflow_defaults_resolve_fiscal_year_and_ytd_dynamically():
    values = {
        "cashflow_fiscal_year_start_month": "4",
        "cashflow_default_period": "ytd",
        "cashflow_custom_start": "",
        "cashflow_custom_end": "",
    }
    result = settings_router._resolved_cashflow_defaults(values, pd.Timestamp("2026-09-29").date())
    assert result["resolved_period"] == {
        "start": "2026-04-01",
        "end": "2026-09-29",
        "group_by": "month",
    }

    values["cashflow_default_period"] = "financial_year"
    result = settings_router._resolved_cashflow_defaults(values, pd.Timestamp("2026-02-10").date())
    assert result["resolved_period"]["start"] == "2025-04-01"
    assert result["resolved_period"]["end"] == "2026-03-31"


def test_cashflow_defaults_fall_back_to_safe_ytd_for_invalid_stored_values():
    result = settings_router._resolved_cashflow_defaults(
        {
            "cashflow_fiscal_year_start_month": "not-a-month",
            "cashflow_default_period": "unknown",
            "cashflow_custom_start": "not-a-date",
            "cashflow_custom_end": "also-not-a-date",
        },
        pd.Timestamp("2026-09-29").date(),
    )
    assert result["default_period"] == "ytd"
    assert result["resolved_period"]["start"] == "2026-01-01"
    assert result["resolved_period"]["end"] == "2026-09-29"



def test_preview_report_returns_controls_and_category_preview():
    with patch.object(reconciliation, "_require_active_client"), \
         patch.object(reconciliation, "_stored_group_label", return_value="Bank"), \
         patch.object(reconciliation, "_saved_codes", side_effect=_saved_codes), \
         patch.object(reconciliation, "load_ledger_report_source", return_value=_source()), \
         patch.object(reconciliation, "load_reconciliation_balance_source", return_value=_balance_source()), \
         patch.object(reconciliation, "build_group_summary", return_value=_summary()):
        response = reconciliation.preview_reconciliation_report(
            7,
            "Bank",
            "2026-01-01",
            "2026-01-31",
            journal_upload_id=21,
            journal_template_id=None,
            session=None,
        )

    assert response["can_download"] is True
    assert response["controls"]["ledger_balance"]["status"] == "Balanced"
    assert response["controls"]["categories"]["status"] == "Clear"
    assert response["controls"]["reconciliation"]["status"] == "Matched"
    assert response["preview"]["category_totals"][0]["category"] == "Customers"


def test_preview_report_blocks_download_when_balance_evidence_is_missing():
    diagnostics = ({"code": "missing_closing_months", "severity": "error", "message": "Closing balances are missing."},)
    with patch.object(reconciliation, "_require_active_client"), \
         patch.object(reconciliation, "_stored_group_label", return_value="Bank"), \
         patch.object(reconciliation, "_saved_codes", side_effect=_saved_codes), \
         patch.object(reconciliation, "load_ledger_report_source", return_value=_source()), \
         patch.object(reconciliation, "load_reconciliation_balance_source", return_value=_balance_source(False, diagnostics)):
        response = reconciliation.preview_reconciliation_report(
            7,
            "Bank",
            "2026-01-01",
            "2026-01-31",
            journal_upload_id=21,
            journal_template_id=None,
            session=None,
        )

    assert response["can_download"] is False
    assert response["controls"]["reconciliation"]["status"] == "Blocked"
    assert any("Closing balances are missing" in reason for reason in response["blocking_reasons"])


def test_cashflow_drilldown_rollups_reconcile_and_split_treasury_legs():
    journal = pd.DataFrame(
        [
            {'voucher_id': 'R1', 'date': '2026-01-05', 'account_code': 'BANK', 'account_name': 'Main Bank', 'voucher_detail': 'Receipt', 'debit': 100, 'credit': 0},
            {'voucher_id': 'R1', 'date': '2026-01-05', 'account_code': 'AR', 'account_name': 'Customers', 'voucher_detail': 'Receipt', 'debit': 0, 'credit': 100},
            {'voucher_id': 'R2', 'date': '2026-01-06', 'account_code': 'CASH', 'account_name': 'Main Cash', 'voucher_detail': 'Payment', 'debit': 0, 'credit': 40},
            {'voucher_id': 'R2', 'date': '2026-01-06', 'account_code': 'EXP', 'account_name': 'Operating expense', 'voucher_detail': 'Payment', 'debit': 40, 'credit': 0},
        ]
    )
    coa = pd.DataFrame(
        [
            {'account_code': 'BANK', 'account_title': 'Main Bank', 'account_type': 'Bank', 'category': ''},
            {'account_code': 'CASH', 'account_title': 'Main Cash', 'account_type': 'Cash', 'category': ''},
            {'account_code': 'AR', 'account_title': 'Customers', 'account_type': 'Receivable', 'category': 'Received from Customers'},
            {'account_code': 'EXP', 'account_title': 'Operating expense', 'account_type': 'Expense', 'category': 'Operating expenses'},
        ]
    )

    result = build_cashflow_summary(
        journal,
        coa,
        cash_account_codes=['CASH'],
        bank_account_codes=['BANK'],
        period_start='2026-01-01',
        period_end='2026-01-31',
        include_drilldown=True,
    )

    rollups = result['cashflow_ledger_rollups']
    assert result['cashflow_drilldown_prepared'] is True
    assert set(rollups['treasury_type']) == {'Bank', 'Cash'}
    assert rollups.groupby('category')['amount'].sum().to_dict() == {
        'Operating expenses': -40.0,
        'Received from Customers': 100.0,
    }
    assert all(row['status'] == 'Matched' for row in result['cashflow_drilldown_controls']['category_controls'])
    split = result['cashflow_drilldown_controls']['cash_bank_split']
    assert split['bank_amount'] == 100.0
    assert split['cash_amount'] == -40.0
    assert split['mixed_or_unresolved_line_count'] == 0
    assert split['status'] == 'Matched'


def test_cashflows_preview_exposes_compact_drilldown_contract():
    summary = _cashflow_summary()
    summary['cashflow_ledger_rollups'] = pd.DataFrame(
        [{'category': 'Customers', 'period_key': '2026-Q1', 'period_label': 'Q1 2026', 'account_code': 'AR', 'account_name': 'Customers', 'treasury_type': 'Bank', 'amount': 125.0, 'line_count': 1}]
    )
    summary['cashflow_drilldown_controls'] = {'cash_bank_split': {'status': 'Matched'}}
    summary['cashflow_drilldown_prepared'] = True

    preview = reconciliation._cashflow_preview(summary, {'drilldown'})

    assert len(preview['ledger_rollups']) == 1
    assert preview['ledger_rollups'][0]['treasury_type'] == 'Bank'
    assert preview['drilldown_controls']['cash_bank_split']['status'] == 'Matched'
    assert preview['detail_sections_loaded'] == ['drilldown']


def test_cashflow_drilldown_cache_requires_current_payload_version():
    legacy = {'metadata': {'drilldown_details_prepared': True, 'drilldown_details_version': 0}, 'summary': _cashflow_summary()}
    current = {'metadata': {'summary_payload_version': CASHFLOW_SUMMARY_PAYLOAD_VERSION, 'drilldown_details_prepared': True, 'drilldown_details_version': CASHFLOW_DRILLDOWN_DETAILS_VERSION}, 'summary': _cashflow_summary()}
    stale_summary = {'metadata': {'summary_payload_version': CASHFLOW_SUMMARY_PAYLOAD_VERSION - 1}, 'summary': _cashflow_summary()}

    assert reconciliation._cashflow_cache_can_serve_details(legacy, {'drilldown'}) is False
    assert reconciliation._cashflow_cache_can_serve_details(stale_summary, None) is False
    assert reconciliation._cashflow_cache_can_serve_details(current, {'drilldown'}) is True
    assert reconciliation._cashflow_cache_can_serve_details(current, None) is True


def test_cashflow_drilldown_route_filters_and_paginates_server_side():
    result = {
        'period_start': '2026-01-01',
        'period_end': '2026-01-31',
        'group_by': 'month',
        'preview': {
            'ledger_rollups': [
                {'category': 'Customers', 'period_key': '2026-01', 'period_label': 'Jan 2026', 'account_code': 'AR1', 'account_name': 'Main Bank customer', 'treasury_type': 'Bank', 'amount': 100.0, 'line_count': 1},
                {'category': 'Customers', 'period_key': '2026-01', 'period_label': 'Jan 2026', 'account_code': 'AR2', 'account_name': 'Cash customer', 'treasury_type': 'Cash', 'amount': 50.0, 'line_count': 1},
                {'category': 'Payments', 'period_key': '2026-01', 'period_label': 'Jan 2026', 'account_code': 'EXP', 'account_name': 'Operating expense', 'treasury_type': 'Bank', 'amount': -75.0, 'line_count': 2},
            ],
            'drilldown_controls': {'cash_bank_split': {'status': 'Matched'}},
        },
        'source': {'ready': True, 'metadata': {'cashflow_report_revision': 7}},
        'blocking_reasons': [],
    }
    with patch.object(reconciliation, '_build_cashflow_result', return_value=result) as builder:
        response = reconciliation.cashflow_drilldown(
            7,
            '2026-01-01',
            '2026-01-31',
            group_by='month',
            treasury_view='combined',
            search='main',
            treasury_type='Bank',
            page=1,
            limit=1,
            session=None,
        )

    assert response['total_rows'] == 1
    assert response['page_count'] == 1
    assert response['rows'][0]['account_code'] == 'AR1'
    assert response['controls']['cash_bank_split']['status'] == 'Matched'
    assert response['source']['metadata']['cashflow_report_revision'] == 7
    assert builder.call_args.kwargs['detail_sections'] == {'drilldown'}


def test_cashflow_drilldown_returns_baseline_rows_for_unfiltered_request():
    result = {
        'period_start': '2026-01-01',
        'period_end': '2026-01-31',
        'group_by': 'month',
        'preview': {
            'ledger_rollups': [
                {'category': 'Customers', 'period_key': '2026-01', 'period_label': 'Jan 2026', 'account_code': 'AR1', 'account_name': 'Main customer', 'treasury_type': 'Bank', 'amount': 100.0, 'line_count': 1},
            ],
        },
        'source': {'ready': True},
        'blocking_reasons': [],
    }
    with patch.object(reconciliation, '_build_cashflow_result', return_value=result):
        response = reconciliation.cashflow_drilldown(
            7,
            '2026-01-01',
            '2026-01-31',
            group_by='month',
            treasury_view='combined',
            search='',
            category='',
            account_head='',
            treasury_type='',
            period_key='',
            page=1,
            limit=50,
            session=None,
        )

    assert response['base_rows'] == result['preview']['ledger_rollups']
    assert response['rows'] == result['preview']['ledger_rollups']


def test_cashflow_drilldown_filters_account_head_and_returns_scoped_options():
    result = {
        'period_start': '2026-01-01',
        'period_end': '2026-01-31',
        'group_by': 'month',
        'preview': {
            'ledger_rollups': [
                {'category': 'Customers', 'period_key': '2026-01', 'period_label': 'Jan 2026', 'account_code': 'AR1', 'account_name': 'Main Bank customer', 'treasury_type': 'Bank', 'amount': 100.0, 'line_count': 1},
                {'category': 'Customers', 'period_key': '2026-01', 'period_label': 'Jan 2026', 'account_code': 'AR2', 'account_name': 'Cash customer', 'treasury_type': 'Cash', 'amount': 50.0, 'line_count': 1},
                {'category': 'Payments', 'period_key': '2026-01', 'period_label': 'Jan 2026', 'account_code': 'EXP', 'account_name': 'Operating expense', 'treasury_type': 'Bank', 'amount': -75.0, 'line_count': 2},
            ],
        },
        'source': {'ready': True},
        'blocking_reasons': [],
    }
    with patch.object(reconciliation, '_build_cashflow_result', return_value=result):
        response = reconciliation.cashflow_drilldown(
            7,
            '2026-01-01',
            '2026-01-31',
            group_by='month',
            treasury_view='combined',
            category='Customers',
            search='',
            account_head='Cash customer / AR2',
            treasury_type='',
            period_key='',
            page=1,
            limit=1,
            session=None,
        )

    assert response['total_rows'] == 1
    assert response['page_count'] == 1
    assert response['rows'][0]['account_code'] == 'AR2'
    assert response['base_rows'] is None
    assert {option['value'] for option in response['account_head_options']} == {
        'Main Bank customer / AR1',
        'Cash customer / AR2',
        'Operating expense / EXP',
    }

def test_cashflows_preview_passes_grouping_and_returns_period_descriptors():
    configuration = {
        "source_template_id": 9,
        "cash_account_codes": [],
        "bank_account_codes": ["B1"],
        "match_internal_transfers": False,
        "receipt_presentation": "net",
        "customer_payment_roles": {
            "customer_receipt": ["Received from customers"],
            "bank_charge": ["Bank Charges"],
            "withholding_tax": ["Withholding tax on export"],
            "other_deduction": ["Other deductions"],
        },
    }
    with (
        patch.object(reconciliation, "_require_active_client"),
        patch.object(reconciliation, "_cashflow_configuration", return_value=configuration),
        patch.object(reconciliation, "load_ledger_report_source", return_value=_source()),
        patch.object(reconciliation, "build_cashflow_summary", return_value=_cashflow_summary()) as builder,
    ):
        response = reconciliation.preview_cashflows(
            7,
            "2026-02-10",
            "2026-04-18",
            group_by="quarter",
            details="summary",
            session=None,
        )

    assert response["group_by"] == "quarter"
    assert response["preview"]["group_by"] == "quarter"
    assert response["preview"]["periods"][1]["label"] == "Q2 2026"
    assert response["preview"]["category_totals"][0]["2026-Q1"] == 125.0
    assert builder.call_args.kwargs["group_by"] == "quarter"
    assert builder.call_args.kwargs["group_by"] == response["group_by"]
    assert builder.call_args.kwargs["cashflow_role_categories"] == configuration["customer_payment_roles"]


def test_cashflow_preview_exposes_customer_wise_rows_and_selected_customer_filter():
    summary = _cashflow_summary()
    summary["customer_payment_customer_totals"] = pd.DataFrame(
        [
            {
                "customer_account_code": "CUST-1",
                "customer_account_name": "Customer One",
                "statement_eligible_actual_settlement": 100.0,
                "status": "Matched",
            },
            {
                "customer_account_code": "CUST-2",
                "customer_account_name": "Customer Two",
                "statement_eligible_actual_settlement": 50.0,
                "status": "Review required",
            },
        ]
    )
    summary["customer_payment_period_totals"] = pd.DataFrame(
        [
            {
                "period_key": "2026-Q1",
                "period_label": "Q1 2026",
                "statement_eligible_actual_settlement": 100.0,
                "main_received_from_customers_total": 100.0,
                "difference": 0.0,
                "status": "Matched",
            },
            {
                "period_key": "2026-Q2",
                "period_label": "Q2 2026",
                "statement_eligible_actual_settlement": 0.0,
                "main_received_from_customers_total": 50.0,
                "difference": -50.0,
                "status": "Review required",
            },
        ]
    )
    summary["customer_payment_reconciliation"] = {
        "statement_eligible_customer_total": 100.0,
        "main_received_from_customers_total": 100.0,
        "difference": 0.0,
        "status": "Matched",
    }

    response = reconciliation._cashflow_preview(
        summary,
        {"customer_payments"},
        "combined",
        "CUST-2",
    )

    assert response["customer_payment_customer_options"] == [
        {"account_code": "CUST-1", "account_name": "Customer One"},
        {"account_code": "CUST-2", "account_name": "Customer Two"},
    ]
    assert [row["customer_account_code"] for row in response["customer_payment_customer_totals"]] == ["CUST-2"]
    assert response["customer_payment_reconciliation"]["status"] == "Matched"
    assert [row["period_key"] for row in response["customer_payment_period_totals"]] == ["2026-Q1", "2026-Q2"]
    assert response["customer_payment_period_totals"][0]["difference"] == 0.0
    assert response["customer_payment_selected_account_code"] == "CUST-2"


def test_cashflows_preview_uses_client_data_source_without_read_model_lookup():
    configuration = {
        "source_template_id": 9,
        "cash_account_codes": [],
        "bank_account_codes": ["B1"],
        "match_internal_transfers": False,
        "receipt_presentation": "net",
    }
    source = _source()
    source.metadata["database_totals"] = {
        "client_id": 7, "template_id": 9, "upload_id": None,
        "period_start": "2026-02-10", "period_end": "2026-04-18",
        "row_count": 17, "voucher_count": 6,
    }
    with (
        patch.object(reconciliation, "_require_active_client"),
        patch.object(reconciliation, "_cashflow_configuration", return_value=configuration),
        patch.object(reconciliation, "load_ledger_report_source", return_value=source) as loader,
        patch.object(reconciliation, "build_cashflow_summary", return_value=_cashflow_summary()),
    ):
        response = reconciliation.preview_cashflows(
            7,
            "2026-02-10",
            "2026-04-18",
            group_by="quarter",
            details="summary",
            session=None,
        )

    loader.assert_called_once()
    assert response["preview"]["group_by"] == "quarter"
    assert response["preview"]["category_totals"][0]["2026-Q1"] == 125.0
    assert response["database_totals"] == source.metadata["database_totals"]


def test_cashflows_preview_builds_child_sections_from_client_data_source():
    configuration = {
        "source_template_id": 9,
        "cash_account_codes": [],
        "bank_account_codes": ["B1"],
        "match_internal_transfers": False,
        "receipt_presentation": "net",
    }
    summary = _cashflow_summary()
    summary["transfer_details"] = pd.DataFrame([{
        "voucher_id": "TR-1",
        "period_label": "Q1 2026",
        "transfer_type": "Bank to Cash",
        "amount": 20.0,
        "matched": True,
    }])
    summary["deduction_details"] = pd.DataFrame([{
        "period_label": "Q2 2026",
        "category": "Withholding",
        "account_name": "Tax",
        "amount": 5.0,
    }])
    summary["categories_needing_review"] = pd.DataFrame([{
        "voucher_id": "JV-1",
        "period_label": "Q2 2026",
        "account_code": "4100",
        "account_name": "Other",
        "fallback_category": "Review",
    }])
    with (
        patch.object(reconciliation, "_require_active_client"),
        patch.object(reconciliation, "_cashflow_configuration", return_value=configuration),
        patch.object(reconciliation, "load_ledger_report_source", return_value=_source()) as loader,
        patch.object(reconciliation, "build_cashflow_summary", return_value=summary),
    ):
        response = reconciliation.preview_cashflows(
            7,
            "2026-02-10",
            "2026-04-18",
            group_by="quarter",
            details="transfers,deductions,review",
            session=None,
        )

    loader.assert_called_once()
    assert len(response["preview"]["transfer_details"]) == 1
    assert len(response["preview"]["deduction_details"]) == 1
    assert len(response["preview"]["categories_needing_review"]) == 1


def test_cashflows_preview_falls_back_for_legacy_summary_cache_on_child_request():
    configuration = {
        "source_template_id": 9,
        "cash_account_codes": [],
        "bank_account_codes": ["B1"],
        "match_internal_transfers": False,
        "receipt_presentation": "net",
    }
    cached = {
        "metadata": {
            "source_upload_ids": [21],
            "selected_template_upload_ids": [21],
            "source_row_count": 4,
            "valid_row_count": 4,
            "child_details_prepared": True,
            "summary_read_model": "hit",
        },
        "summary": _cashflow_summary(),
    }
    with (
        patch.object(reconciliation, "_require_active_client"),
        patch.object(reconciliation, "_cashflow_configuration", return_value=configuration),
        patch.object(reconciliation, "load_cashflow_summary_read_model", return_value=cached),
        patch.object(reconciliation, "load_cashflow_prepared_summary", return_value=None),
        patch.object(reconciliation, "load_ledger_report_source", return_value=_source()) as loader,
        patch.object(reconciliation, "build_cashflow_summary", return_value=_cashflow_summary()) as builder,
    ):
        reconciliation.preview_cashflows(
            7,
            "2026-02-10",
            "2026-04-18",
            group_by="quarter",
            details="transfers,deductions,review",
            session=None,
        )

    loader.assert_called_once()
    builder.assert_called_once()


def test_cashflows_preview_rejects_previous_child_details_version():
    cached = {
        "metadata": {
            "child_details_prepared": True,
            "child_details_version": CASHFLOW_CHILD_DETAILS_VERSION - 1,
        },
        "summary": _cashflow_summary(),
    }

    assert CASHFLOW_CHILD_DETAILS_VERSION == 9
    assert reconciliation._cashflow_cache_can_serve_details(cached, {"customer_payments"}) is False


@pytest.mark.skip(reason="Cashflow summary read models are migration-only after the client_data source cutover")
def test_cashflow_summary_read_model_round_trip_preserves_period_totals():
    from services import ledger_report_source

    writes = []
    summary = _cashflow_summary()
    summary["transfer_details"] = pd.DataFrame([{
        "voucher_id": "TR-1",
        "date": pd.Timestamp("2026-02-12"),
        "month": "2026-02",
        "period_key": "2026-Q1",
        "period_label": "Q1 2026",
        "transfer_type": "Bank to Cash",
        "amount": 20.0,
        "matched": True,
        "voucher_detail": "Transfer",
    }])
    summary["customer_payment_details"] = pd.DataFrame([{
        "voucher_id": "CP-1",
        "date": pd.Timestamp("2026-02-13"),
        "period_key": "2026-Q1",
        "period_label": "Q1 2026",
        "customer_account_name": "Customer Export",
        "bank_account_name": "Export Bank",
        "method": "Gross",
        "customer_gross_amount": 100.0,
        "net_bank_settlement": 97.0,
        "status": "Matched",
    }])
    summary["deduction_details"] = pd.DataFrame([{
        "category": "Withholding",
        "month": "2026-04",
        "period_key": "2026-Q2",
        "period_label": "Q2 2026",
        "amount": 5.0,
        "account_code": "TAX",
        "account_name": "Tax",
        "date": pd.Timestamp("2026-04-04"),
        "presentation": "shown_separately",
    }])
    summary["categories_needing_review"] = pd.DataFrame([{
        "voucher_id": "JV-1",
        "date": pd.Timestamp("2026-04-04"),
        "period_key": "2026-Q2",
        "period_label": "Q2 2026",
        "account_code": "4100",
        "account_name": "Other",
        "voucher_detail": "Review",
        "debit": 5.0,
        "credit": 0.0,
        "fallback_category": "Review",
    }])
    ledger_report_source.store_cashflow_summary_read_model(
        7,
        journal_template_id=9,
        source_upload_ids=[21],
        configuration_version=4,
        report_revision=7,
        period_start="2026-02-10",
        period_end="2026-04-18",
        group_by="quarter",
        summary=summary,
        write_fn=lambda query, params: writes.append((query, params)),
    )

    payload = json.loads(writes[0][1][-1])
    assert payload["summary_payload_version"] == CASHFLOW_SUMMARY_PAYLOAD_VERSION
    assert "customer_payment_customer_totals" in payload
    assert "customer_payment_period_totals" in payload
    assert "customer_payment_reconciliation" in payload

    def query(sql, params):
        if "FROM uploaded_files" in sql:
            return [{"upload_id": 21, "file_name": "period.xlsx", "upload_date": "2026-04-01"}]
        return [{
            "summary_payload": payload,
            "configuration_version": 4,
            "report_revision": 7,
            "created_at": "2026-09-29T00:00:00+00:00",
        }]

    cached = ledger_report_source.load_cashflow_summary_read_model(
        7,
        journal_template_id=9,
        period_start="2026-02-10",
        period_end="2026-04-18",
        group_by="quarter",
        receipt_presentation="net",
        query_fn=query,
    )

    assert cached["summary"]["category_totals"].loc["Customers", "2026-Q1"] == 125.0
    assert cached["metadata"]["summary_read_model"] == "hit"
    assert cached["metadata"]["cashflow_report_revision"] == 7
    assert cached["metadata"]["child_details_prepared"] is True
    assert cached["metadata"]["child_details_version"] == CASHFLOW_CHILD_DETAILS_VERSION
    assert cached["metadata"]["summary_payload_version"] == CASHFLOW_SUMMARY_PAYLOAD_VERSION
    assert cached["summary"]["transfer_details"].loc[0, "amount"] == 20.0
    assert cached["summary"]["customer_payment_details"].loc[0, "method"] == "Gross"
    assert "period_key" in cached["summary"]["customer_payment_period_totals"].columns
    assert cached["summary"]["deduction_details"].loc[0, "account_name"] == "Tax"
    assert cached["summary"]["categories_needing_review"].loc[0, "account_code"] == "4100"

def test_prepared_daily_snapshot_rolls_up_to_requested_months():
    from services.ledger_report_source import _cashflow_daily_rollup

    payload = {
        "periods": [
            {"key": "2026-01-01", "start": "2026-01-01", "end": "2026-01-01", "label": "01-Jan-26"},
            {"key": "2026-01-02", "start": "2026-01-02", "end": "2026-01-02", "label": "02-Jan-26"},
            {"key": "2026-02-01", "start": "2026-02-01", "end": "2026-02-01", "label": "01-Feb-26"},
        ],
        "categories": [{"category": "Customers", "values": {"2026-01-01": 10, "2026-01-02": 15, "2026-02-01": 20}}],
        "child_details_prepared": True,
        "child_details_version": CASHFLOW_CHILD_DETAILS_VERSION,
        "transfer_details": [{
            "voucher_id": "TR-1",
            "date": "2026-01-02",
            "month": "2026-01",
            "period_key": "2026-01-02",
            "period_label": "02-Jan-26",
            "transfer_type": "Bank to Cash",
            "amount": 20.0,
            "matched": True,
            "voucher_detail": "Transfer",
        }],
        "customer_payment_details": [{
            "voucher_id": "CP-1",
            "date": "2026-01-02",
            "period_key": "2026-01-02",
            "period_label": "02-Jan-26",
            "customer_account_name": "Customer Export",
            "bank_account_name": "Export Bank",
            "method": "Gross",
            "customer_gross_amount": 100.0,
            "net_bank_settlement": 97.0,
            "status": "Matched",
        }],
        "deduction_details": [{
            "category": "Withholding",
            "month": "2026-02",
            "period_key": "2026-02-01",
            "period_label": "01-Feb-26",
            "amount": 5.0,
            "account_code": "TAX",
            "account_name": "Tax",
            "date": "2026-02-01",
            "presentation": "shown_separately",
        }],
        "categories_needing_review": [{
            "voucher_id": "JV-1",
            "date": "2026-02-01",
            "period_key": "2026-02-01",
            "period_label": "01-Feb-26",
            "account_code": "4100",
            "account_name": "Other",
            "voucher_detail": "Review",
            "debit": 5.0,
            "credit": 0.0,
            "fallback_category": "Review",
        }],
        "period_metrics": {
            "2026-01-01": {"source_line_count": 2, "selected_voucher_count": 1, "internal_transfer_count": 0, "category_review_count": 0},
            "2026-01-02": {"source_line_count": 2, "selected_voucher_count": 1, "internal_transfer_count": 1, "category_review_count": 0},
            "2026-02-01": {"source_line_count": 2, "selected_voucher_count": 1, "internal_transfer_count": 0, "category_review_count": 1},
        },
        "receipt_presentation": "net",
    }

    summary = _cashflow_daily_rollup(payload, "2026-01-01", "2026-02-28", "month")

    assert summary["category_totals"].loc["Customers", "2026-01"] == 25
    assert summary["category_totals"].loc["Customers", "2026-02"] == 20
    assert summary["selected_voucher_count"] == 3
    assert summary["internal_transfer_count"] == 1
    assert summary["category_review_count"] == 1
    assert summary["transfer_details"].loc[0, "period_key"] == "2026-01"
    assert summary["customer_payment_details"].loc[0, "period_key"] == "2026-01"
    assert summary["deduction_details"].loc[0, "period_key"] == "2026-02"
    assert summary["categories_needing_review"].loc[0, "period_key"] == "2026-02"


def test_cashflows_preview_does_not_use_prepared_snapshot_when_cache_misses():
    configuration = {
        "source_template_id": 9,
        "cash_account_codes": [],
        "bank_account_codes": ["B1"],
        "match_internal_transfers": False,
        "receipt_presentation": "net",
        "cashflow_report_revision": 7,
    }
    with (
        patch.object(reconciliation, "_require_active_client"),
        patch.object(reconciliation, "_cashflow_configuration", return_value=configuration),
        patch.object(reconciliation, "load_ledger_report_source", return_value=_source()) as loader,
        patch.object(reconciliation, "build_cashflow_summary", return_value=_cashflow_summary()),
    ):
        response = reconciliation.preview_cashflows(
            7,
            "2026-02-10",
            "2026-04-18",
            group_by="quarter",
            details="summary",
            session=None,
        )

    loader.assert_called_once()
    assert response["preview"]["category_totals"][0]["2026-Q1"] == 125.0
    assert response["cashflow_report_revision"] == 7

def test_preparation_builds_one_daily_snapshot_after_committed_source_changes():
    from services import ledger_report_source

    source = SimpleNamespace(
        ready=True,
        journal=pd.DataFrame(),
        coa=pd.DataFrame(),
        metadata={"selected_template_upload_ids": [21]},
    )
    summary = _cashflow_summary()

    def query(sql, params=None):
        if "account_control_cashflow_treasury_accounts" in sql:
            return [{
                "treasury": [{"account_code": "B1", "treasury_type": "bank"}],
                "settings": {"source_template_id": 9, "match_internal_transfers": False, "receipt_presentation": "net"},
                "customer_payment_roles": [
                    {"role_key": "customer_receipt", "label": "Received from customers"},
                    {"role_key": "bank_charge", "label": "Bank Charges"},
                    {"role_key": "withholding_tax", "label": "Withholding tax on export"},
                    {"role_key": "other_deduction", "label": "Other deductions"},
                ],
            }]
        if "SELECT canonical_occurrence_id, upload_id" in sql:
            return []
        if "FROM uploaded_files" in sql:
            return [{"upload_id": 21, "file_name": "period.xlsx", "upload_date": "2026-04-01"}]
        if "MIN(c.transaction_date)" in sql:
            return [{"period_start": "2026-01-01", "period_end": "2026-06-30"}]
        if "FROM cashflow_report_revisions" in sql:
            return [{"revision": 6}]
        raise AssertionError(f"Unexpected preparation read: {sql}")

    with (
        patch.object(ledger_report_source, "load_cashflow_sql_source", return_value=source),
        patch("services.ledger_group_summary.build_cashflow_summary", return_value=summary) as builder,
        patch.object(ledger_report_source, "store_cashflow_summary_read_model") as store,
    ):
        result = ledger_report_source.prepare_cashflow_summary_snapshot(5, query_fn=query)

    assert result["status"] == "prepared"
    builder.assert_called_once()
    assert store.call_args.kwargs["group_by"] == "day"
    assert store.call_args.kwargs["source_upload_ids"] == [21]
    assert store.call_args.kwargs["report_revision"] == 6
    assert builder.call_args.kwargs["cashflow_role_categories"] == {
        "customer_receipt": ["Received from customers"],
        "bank_charge": ["Bank Charges"],
        "withholding_tax": ["Withholding tax on export"],
        "other_deduction": ["Other deductions"],
    }

def test_cashflow_configuration_uses_one_query_and_preserves_sections():
    calls = []

    def query_fn(query, params):
        calls.append((query, params))
        return [{
            "mappings": [{"account_code": "EXP", "category_key": "operating", "label": "Operating expense"}],
            "treasury": [
                {"account_code": "CASH", "treasury_type": "cash"},
                {"account_code": "BANK", "treasury_type": "bank"},
            ],
            "settings": {
                "match_internal_transfers": True,
                "receipt_presentation": "gross",
                "source_upload_id": None,
                "source_template_id": 9,
            },
        }]

    with patch.object(reconciliation, "execute_query_pg", side_effect=query_fn):
        result = reconciliation._cashflow_configuration(7)

    assert len(calls) == 1
    assert calls[0][1] == (7, 7, 7)
    assert result["category_mappings"] == {"EXP": "Operating expense"}
    assert result["cash_account_codes"] == ["CASH"]
    assert result["bank_account_codes"] == ["BANK"]
    assert result["match_internal_transfers"] is True
    assert result["receipt_presentation"] == "gross"
    assert result["source_template_id"] == 9


def test_cashflows_preview_logs_performance_stages(caplog):
    configuration = {
        "source_template_id": 9,
        "cash_account_codes": [],
        "bank_account_codes": ["B1"],
        "match_internal_transfers": False,
        "receipt_presentation": "net",
    }
    with (
        caplog.at_level(logging.INFO, logger="routers.reconciliation"),
        patch.object(reconciliation, "_require_active_client"),
        patch.object(reconciliation, "_cashflow_configuration", return_value=configuration),
        patch.object(reconciliation, "load_ledger_report_source", return_value=_source()),
        patch.object(reconciliation, "build_cashflow_summary", return_value=_cashflow_summary()),
    ):
        response = reconciliation.preview_cashflows(
            7,
            "2026-02-10",
            "2026-04-18",
            group_by="quarter",
            details="summary",
            session=None,
        )

    performance_records = [record for record in caplog.records if record.getMessage().startswith("cashflows_performance ")]
    assert len(performance_records) == 1
    message = performance_records[0].getMessage()
    assert "validation_seconds=" in message
    assert "configuration_seconds=" in message
    assert "source_seconds=" in message
    assert "summary_seconds=" in message
    assert "format_seconds=" in message
    assert "total_seconds=" in message
    assert "source_rows=4" in message
    assert "cache_mode=client_data_source" in message
    assert "cache_hit=False" in message
    assert "read_model=none" in message
    assert "report_revision=1" in message
    assert "authoritative=True" in message
    assert response["preview"]["group_by"] == "quarter"




def test_prepared_drilldown_reads_active_prepared_lines_and_filters_bank():
    def query(sql, params=None):
        if "cashflow_preparation_state" in sql:
            return [{"active_preparation_revision": 17, "status": "ready"}]
        assert "FROM cashflow_prepared_lines" in sql
        return [
            {
                "period_start": "2026-01-05",
                "category": "Customers",
                "account_code": "AR",
                "account_name": "Customer",
                "treasury_type": "Bank",
                "amount": 100,
                "line_count": 2,
            },
            {
                "period_start": "2026-01-05",
                "category": "Customers",
                "account_code": "AR",
                "account_name": "Customer",
                "treasury_type": "Cash",
                "amount": 50,
                "line_count": 1,
            },
        ]

    result = load_cashflow_prepared_drilldown(
        7,
        journal_template_id=9,
        period_start="2026-01-01",
        period_end="2026-01-31",
        group_by="month",
        category="Customers",
        treasury_view="bank",
        query_fn=query,
    )

    assert result["metadata"]["targeted_prepared_lines"] is True
    assert result["metadata"]["preparation_revision"] == 17
    assert result["metadata"]["treasury_filter_stage"] == "prepared_lines_bank"
    assert result["rows"] == [{
        "category": "Customers",
        "period_key": "2026-01",
        "period_label": "Jan-26",
        "account_code": "AR",
        "account_name": "Customer",
        "treasury_type": "Bank",
        "amount": 100,
        "line_count": 2,
    }]

def test_cashflow_drilldown_uses_client_data_report_for_category(caplog):
    full_result = {
        "period_start": "2026-01-01",
        "period_end": "2026-01-31",
        "group_by": "month",
        "treasury_view": "combined",
        "cashflow_report_revision": 4,
        "freshness": {"mode": "client_data_source", "report_revision": 4, "authoritative": True},
        "preview": {
            "ledger_rollups": [{
                "category": "Customers",
                "period_key": "2026-01",
                "period_label": "Jan-26",
                "account_code": "AR1",
                "account_name": "Main Bank customer",
                "treasury_type": "Bank",
                "amount": 125.0,
                "line_count": 2,
            }],
            "drilldown_controls": {},
        },
        "source": {
            "ready": True,
            "metadata": {"cashflow_report_source_mode": "client_data_derived"},
            "quality": {"source_row_count": 2},
        },
        "blocking_reasons": [],
        "database_totals": {
            "client_id": 7, "template_id": 9, "upload_id": None,
            "period_start": "2026-01-01", "period_end": "2026-01-31",
            "row_count": 17, "voucher_count": 6,
        },
    }
    configuration = {"source_template_id": 9, "cashflow_report_revision": 4}
    with (
        caplog.at_level(logging.INFO, logger="routers.reconciliation"),
        patch.object(reconciliation, "_cashflow_configuration", return_value=configuration),
        patch.object(reconciliation, "load_cashflow_prepared_drilldown") as targeted_loader,
        patch.object(reconciliation, "_build_cashflow_result", return_value=full_result) as full_report,
    ):
        response = reconciliation.cashflow_drilldown(
            7,
            "2026-01-01",
            "2026-01-31",
            group_by="month",
            category="Customers",
            treasury_view="combined",
            page=1,
            limit=50,
            session=None,
        )

    full_report.assert_called_once()
    targeted_loader.assert_not_called()
    assert response["rows"][0]["account_code"] == "AR1"
    assert response["source"]["metadata"]["cashflow_report_source_mode"] == "client_data_derived"
    assert response["database_totals"] == full_result["database_totals"]
    assert response["database_totals"]["row_count"] != len(response["rows"])
    record = next(record for record in caplog.records if record.getMessage().startswith("cashflows_drilldown_performance "))
    assert "projection_mode=full_report" in record.getMessage()
    assert "cache_mode=client_data_source" in record.getMessage()


def test_cashflow_drilldown_logs_endpoint_timing(caplog):
    configuration = {
        "source_template_id": 9,
        "cash_account_codes": [],
        "bank_account_codes": ["B1"],
        "match_internal_transfers": False,
        "receipt_presentation": "net",
    }
    summary = _cashflow_summary()
    summary["cashflow_ledger_rollups"] = pd.DataFrame([{
        "category": "Customers",
        "account_code": "B1",
        "account_name": "Bank One",
        "period_key": "2026-Q1",
        "period_label": "Q1 2026",
        "treasury_type": "Bank",
    }])
    with (
        caplog.at_level(logging.INFO, logger="routers.reconciliation"),
        patch.object(reconciliation, "_require_active_client"),
        patch.object(reconciliation, "_cashflow_configuration", return_value=configuration),
        patch.object(reconciliation, "load_cashflow_summary_read_model", return_value=None) as exact_read_model,
        patch.object(reconciliation, "load_cashflow_prepared_summary", return_value=None) as prepared_snapshot,
        patch.object(reconciliation, "load_ledger_report_source", return_value=_source()),
        patch.object(reconciliation, "build_cashflow_summary", return_value=summary),
        patch.object(reconciliation, "store_cashflow_summary_read_model"),
    ):
        response = reconciliation.cashflow_drilldown(
            7,
            "2026-02-10",
            "2026-04-18",
            group_by="quarter",
            treasury_view="combined",
            search="customer",
            page=1,
            limit=1,
            session=None,
        )

    performance_records = [
        record for record in caplog.records
        if record.getMessage().startswith("cashflows_drilldown_performance ")
    ]
    assert len(performance_records) == 1
    message = performance_records[0].getMessage()
    assert "build_seconds=" in message
    assert "filter_seconds=" in message
    assert "total_seconds=" in message
    assert "filter_count=1" in message
    assert "total_rows=1" in message
    assert "returned_rows=1" in message
    assert "cache_mode=client_data_source" in message
    assert "cache_hit=False" in message
    assert "report_revision=1" in message
    exact_read_model.assert_not_called()
    prepared_snapshot.assert_not_called()
    assert response["page_count"] == 1

def test_cashflows_preview_accepts_combined_child_sections(caplog):
    configuration = {
        "source_template_id": 9,
        "cash_account_codes": [],
        "bank_account_codes": ["B1"],
        "match_internal_transfers": False,
        "receipt_presentation": "net",
    }
    summary = _cashflow_summary()
    summary["transfer_details"] = pd.DataFrame([{
        "voucher_id": "TR-1",
        "period_label": "Q1 2026",
        "transfer_type": "Bank to Cash",
        "amount": 20.0,
        "matched": True,
    }])
    summary["customer_payment_details"] = pd.DataFrame([{
        "voucher_id": "CP-1",
        "period_label": "Q1 2026",
        "customer_account_name": "Customer Export",
        "bank_account_name": "Export Bank",
        "method": "Gross",
        "customer_gross_amount": 100.0,
        "net_bank_settlement": 97.0,
        "status": "Matched",
    }])
    summary["deduction_details"] = pd.DataFrame([{
        "period_label": "Q2 2026",
        "category": "Withholding",
        "account_name": "Tax",
        "amount": 5.0,
    }])
    summary["categories_needing_review"] = pd.DataFrame([{
        "voucher_id": "JV-1",
        "period_label": "Q2 2026",
        "account_code": "4100",
        "account_name": "Other",
        "fallback_category": "Review",
    }])
    with (
        caplog.at_level(logging.INFO, logger="routers.reconciliation"),
        patch.object(reconciliation, "_require_active_client"),
        patch.object(reconciliation, "_cashflow_configuration", return_value=configuration),
        patch.object(reconciliation, "load_ledger_report_source", return_value=_source()),
        patch.object(reconciliation, "build_cashflow_summary", return_value=summary) as builder,
    ):
        response = reconciliation.preview_cashflows(
            7,
            "2026-02-10",
            "2026-04-18",
            group_by="quarter",
            details="transfers,customer_payments,deductions,review",
            session=None,
        )

    builder.assert_called_once()
    assert response["preview"]["detail_sections_loaded"] == ["customer_payments", "deductions", "review", "transfers"]
    assert len(response["preview"]["transfer_details"]) == 1
    assert len(response["preview"]["customer_payment_details"]) == 1
    assert len(response["preview"]["deduction_details"]) == 1
    assert len(response["preview"]["categories_needing_review"]) == 1
    performance_records = [record for record in caplog.records if record.getMessage().startswith("cashflows_performance ")]
    assert "details=customer_payments,deductions,review,transfers" in performance_records[0].getMessage()


def test_download_report_returns_professional_xlsx_when_controls_pass():
    result = {
        "can_download": True,
        "blocking_reasons": [],
        "group_label": "Bank",
        "period_start": "2026-01-01",
        "period_end": "2026-01-31",
        "_summary": _summary(),
        "_cross_group_check": None,
        "_opening_balances": {"B1": 1000},
    }
    with patch.object(reconciliation, "_build_report_result", return_value=result), \
         patch.object(reconciliation, "export_reconciliation_xlsx", return_value=b"xlsx"):
        response = reconciliation.download_reconciliation_report(
            7,
            "Bank",
            "2026-01-01",
            "2026-01-31",
            journal_upload_id=21,
            journal_template_id=None,
            session=None,
        )
    assert response.media_type == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    assert "Bank_Monthly_Summary_2026-01_to_2026-01.xlsx" in response.headers["content-disposition"]

def test_cashflow_export_preserves_visible_periods_and_totals():
    preview = {
        "periods": [
            {"key": "2026-Q1", "start": "2026-01-01", "end": "2026-03-31", "label": "Q1 2026"},
            {"key": "2026-Q2", "start": "2026-04-01", "end": "2026-06-30", "label": "Q2 2026"},
        ],
        "group_by": "quarter",
        "category_totals": [
            {"category": "Customers", "2026-Q1": 125.0, "2026-Q2": 50.0, "Total": 175.0},
            {"category": "Vendors", "2026-Q1": -80.0, "2026-Q2": -20.0, "Total": -100.0},
        ],
        "ledger_rollups": [
            {"category": "Customers", "period_key": "2026-Q1", "period_label": "Q1 2026", "account_code": "AR", "account_name": "Customers", "treasury_type": "Bank", "amount": 125.0, "line_count": 1},
            {"category": "Vendors", "period_key": "2026-Q1", "period_label": "Q1 2026", "account_code": "AP", "account_name": "Vendors", "treasury_type": "Cash", "amount": -80.0, "line_count": 2},
        ],
        "drilldown_controls": {"cash_bank_split": {"status": "Matched"}},
        "transfer_details": [{
            "voucher_id": "TR-1",
            "date": "2026-01-05",
            "transferor_account_code": "BA",
            "transferor_account_name": "Bank Alpha",
            "transferor_type": "Bank",
            "transferee_account_code": "CB",
            "transferee_account_name": "Cash Book",
            "transferee_type": "Cash",
            "amount": 20.0,
            "variance": 0.0,
            "status": "Confirmed",
            "voucher_detail": "Bank to cash transfer",
            "matched": True,
        }],
        "deduction_details": [{"period_label": "Q2 2026", "category": "Withholding", "account_name": "Tax", "amount": 5.0}],
        "categories_needing_review": [{"voucher_id": "JV-1", "period_label": "Q2 2026", "account_code": "4100", "account_name": "Other", "fallback_category": "Review"}],
        "selected_voucher_count": 3,
        "internal_transfer_count": 1,
        "deduction_count": 1,
        "category_review_count": 1,
        "receipt_presentation": "net",
    }

    workbook = load_workbook(
        BytesIO(export_cashflow_xlsx(preview, period_start="2026-01-01", period_end="2026-06-30", group_by="quarter")),
        data_only=True,
    )

    summary = workbook["Cashflows Summary"]
    assert summary.cell(4, 1).value == "Receipts — percentages calculated within this section"
    assert [summary.cell(5, column).value for column in range(1, 7)] == ["Category", "Q1 2026", "Q2 2026", "Total", "% of section", "Local total"]
    assert [summary.cell(6, column).value for column in range(1, 7)] == ["Customers", 125, 50, 175, "100.00%", "175 Rs"]
    assert summary.cell(6, 2).number_format == '#,##0;[Red](#,##0);-'
    assert summary.cell(6, 1).font.bold
    assert summary.cell(6, 4).font.bold
    assert summary.cell(6, 4).font.color.rgb.endswith("0070C0")
    assert summary.cell(6, 5).font.color.rgb.endswith("0070C0")
    assert summary.cell(6, 6).font.bold
    assert summary.cell(6, 6).font.color.rgb.endswith("7E350E")
    assert all(summary.cell(4, column).fill.fgColor.rgb.endswith("E2F0D9") for column in range(1, 7))
    assert [summary.cell(7, column).value for column in range(1, 5)] == ["Total Receipts", 125, 50, 175]
    assert all(summary.cell(7, column).font.bold for column in range(1, 7))
    assert summary.cell(7, 4).font.color.rgb.endswith("0070C0")
    assert summary.cell(7, 5).font.color.rgb.endswith("0070C0")
    assert summary.cell(7, 6).font.color.rgb.endswith("7E350E")
    assert all(summary.cell(7, column).fill.fgColor.rgb.endswith("D9EAF7") for column in range(1, 7))
    assert summary.cell(10, 1).value == "Payments — percentages calculated within this section"
    assert [summary.cell(12, column).value for column in range(1, 5)] == ["Vendors", 80, 20, 100]
    transfers = workbook["Internal Transfers"]
    assert [transfers.cell(4, column).value for column in range(1, 9)] == [
        "Date", "Voucher", "Transferor", "Transferee", "Amount", "Variance", "Status", "Description"
    ]
    assert transfers.cell(5, 1).value.date().isoformat() == "2026-01-05"
    assert transfers.cell(5, 2).value == "TR-1"
    assert transfers.cell(5, 3).value == "Bank Alpha (BA) · Bank"
    assert transfers.cell(5, 4).value == "Cash Book (CB) · Cash"
    assert transfers.cell(5, 5).value == 20
    assert transfers.cell(5, 6).value == 0
    assert transfers.cell(5, 6).number_format == '#,##0;[Red](#,##0);-'
    assert transfers.cell(5, 7).value == "Confirmed"
    assert transfers.cell(5, 8).value == "Bank to cash transfer"
    assert transfers.cell(5, 3).alignment.horizontal == "left"
    assert transfers.cell(5, 5).alignment.horizontal == "right"
    drilldown = workbook["Cashflows Ledger Drill-down"]
    assert [drilldown.cell(4, column).value for column in range(1, 8)] == [
        "Category", "Period", "Ledger account", "Account code", "Cash / Bank", "Amount", "Lines"
    ]
    assert [drilldown.cell(5, column).value for column in range(1, 8)] == [
        "Customers", "Q1 2026", "Customers", "AR", "Bank", 125, 1
    ]
    ledgerwise = workbook["Ledgerwise Summary"]
    assert [ledgerwise.cell(4, column).value for column in range(1, 6)] == [
        "Account Code",
        "Account Name/Ledger",
        "Ledger Total Amount (Cash and Bank)",
        "Total Amount as per Cashflows Summary",
        "Difference (if any)",
    ]
    assert [ledgerwise.cell(5, column).value for column in range(1, 6)] == [None, "Customers", 125, 175, -50]
    assert [ledgerwise.cell(6, column).value for column in range(1, 4)] == ["AR", "Customers", 125]
    assert [ledgerwise.cell(7, column).value for column in range(1, 6)] == [None, "Vendors", 80, 100, -20]
    assert [ledgerwise.cell(8, column).value for column in range(1, 4)] == ["AP", "Vendors", 80]
    assert ledgerwise.cell(5, 3).number_format == '#,##0.00;[Red](#,##0.00);0.00'
    assert ledgerwise.cell(5, 2).fill.fgColor.rgb.endswith("D9EAF7")
    assert ledgerwise.freeze_panes == "C5"
    assert all("#REF!" not in str(cell.value) for row in ledgerwise.iter_rows() for cell in row if cell.value is not None)
    controls = workbook["Export Controls"]
    control_values = {controls.cell(row, 1).value: controls.cell(row, 2).value for row in range(5, controls.max_row + 1)}
    assert control_values["Drill-down rows"] == 2
    assert control_values["Drill-down Bank amount"] == 125
    assert control_values["Drill-down Cash amount"] == -80
    assert control_values["Drill-down detail total"] == 45
    assert set(workbook.sheetnames) == {"Cashflows Summary", "Ledgerwise Summary", "Internal Transfers", "Customer Deductions", "Review Required", "Cashflows Ledger Drill-down", "Export Controls"}



def test_cashflow_ledgerwise_summary_reconciles_combined_cash_and_bank_ledgers():
    preview = {
        "periods": [{"key": "2026-01", "label": "Jan 2026"}],
        "group_by": "month",
        "category_totals": [
            {"category": "Payments", "2026-01": -175.0, "Total": -175.0},
            {"category": "Receipts", "2026-01": 30.0, "Total": 30.0},
        ],
        "ledger_rollups": [
            {"category": "Payments", "period_key": "2026-01", "account_code": "P1", "account_name": "Supplier one", "treasury_type": "Bank", "amount": -100.0},
            {"category": "Payments", "period_key": "2026-01", "account_code": "P1", "account_name": "Supplier one", "treasury_type": "Cash", "amount": -50.0},
            {"category": "Payments", "period_key": "2026-01", "account_code": "P2", "account_name": "Supplier two", "treasury_type": "Bank", "amount": -25.0},
            {"category": "Receipts", "period_key": "2026-01", "account_code": "R1", "account_name": "Customer one", "treasury_type": "Bank", "amount": 30.0},
        ],
    }

    workbook = load_workbook(BytesIO(export_cashflow_xlsx(preview)), data_only=True)
    sheet = workbook["Ledgerwise Summary"]
    assert [sheet.cell(row, 2).value for row in range(5, 10)] == ["Payments", "Supplier one", "Supplier two", "Receipts", "Customer one"]
    assert [sheet.cell(5, column).value for column in range(3, 6)] == [175, 175, 0]
    assert [sheet.cell(6, column).value for column in range(1, 4)] == ["P1", "Supplier one", 150]
    assert [sheet.cell(7, column).value for column in range(1, 4)] == ["P2", "Supplier two", 25]
    assert [sheet.cell(8, column).value for column in range(3, 6)] == [30, 30, 0]


def test_cashflow_export_uses_canonical_drilldown_and_active_filters():
    result = {
        "period_start": "2026-01-01",
        "period_end": "2026-01-31",
        "group_by": "month",
        "preview": {
            "periods": [{"key": "2026-01", "start": "2026-01-01", "end": "2026-01-31", "label": "Jan 2026"}],
            "category_totals": [
                {"category": "Customers", "2026-01": 100.0, "Total": 100.0},
                {"category": "Vendors", "2026-01": -20.0, "Total": -20.0},
            ],
            "ledger_rollups": [
                {"category": "Customers", "period_key": "2026-01", "period_label": "Jan 2026", "account_code": "AR", "account_name": "Main customer", "treasury_type": "Bank", "amount": 100.0, "line_count": 1},
                {"category": "Vendors", "period_key": "2026-01", "period_label": "Jan 2026", "account_code": "AP", "account_name": "Cash vendor", "treasury_type": "Cash", "amount": -20.0, "line_count": 1},
            ],
            "drilldown_controls": {"cash_bank_split": {"status": "Matched"}},
        },
        "source": {"ready": True},
        "blocking_reasons": [],
    }
    with (
        patch.object(reconciliation, 'run_read_snapshot_pg', side_effect=lambda callback: callback(None)),
        patch.object(reconciliation, "_build_cashflow_result", return_value=result) as builder,
        patch.object(reconciliation, "export_cashflow_xlsx", return_value=b"xlsx") as exporter,
    ):
        response = reconciliation.export_cashflows_report(
            7,
            "2026-01-01",
            "2026-01-31",
            group_by="month",
            treasury_view="combined",
            search="Main",
            category="Customers",
            account_head="Main customer",
            treasury_type="Bank",
            period_key="2026-01",
            summary_filter="customer",
            hide_zero_rows=True,
            session=None,
        )

    assert response.media_type == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    assert builder.call_args.kwargs["detail_sections"] == {"transfers", "customer_payments", "vendor_payments", "deductions", "review", "drilldown"}
    assert builder.call_args.kwargs['include_balances'] is True
    export_preview = exporter.call_args.args[0]
    assert [row["category"] for row in export_preview["category_totals"]] == ["Customers"]
    assert len(export_preview["ledger_rollups"]) == 1
    assert export_preview["ledger_rollups"][0]["account_code"] == "AR"
    assert export_preview["export_filters"]["hide_zero_rows"] is True
    assert export_preview["export_filters"]["account_head"] == "Main customer"

def test_cashflow_export_keeps_client_data_drilldown_rows():
    result = {
        "period_start": "2026-01-01",
        "period_end": "2026-01-31",
        "group_by": "month",
        "cashflow_report_revision": 4,
        "preview": {
            "periods": [{"key": "2026-01", "start": "2026-01-01", "end": "2026-01-31", "label": "Jan 2026"}],
            "category_totals": [{"category": "Customers", "2026-01": 100.0, "Total": 100.0}],
            "ledger_rollups": [],
        },
        "source": {"ready": True, "metadata": {"journal_template_id": 9}},
        "blocking_reasons": [],
    }
    with (
        patch.object(reconciliation, 'run_read_snapshot_pg', side_effect=lambda callback: callback(None)),
        patch.object(reconciliation, "_build_cashflow_result", return_value=result),
        patch.object(reconciliation, "export_cashflow_xlsx", return_value=b"xlsx") as exporter,
    ):
        response = reconciliation.export_cashflows_report(
            7,
            "2026-01-01",
            "2026-01-31",
            group_by="month",
            category="Customers",
            treasury_view="bank",
            session=None,
    )

    assert response.media_type == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    assert exporter.call_args.args[0]["ledger_rollups"] == []

def test_reports_view_and_dashboard_entry_are_wired():
    root = Path(__file__).resolve().parents[1]
    html = root.joinpath("static", "reconciliation.html").read_text(encoding="utf-8")
    javascript = root.joinpath("static", "reconciliation.js").read_text(encoding="utf-8")
    stylesheet = root.joinpath("static", "style.css").read_text(encoding="utf-8")
    dashboard = root.joinpath("static", "index.html").read_text(encoding="utf-8")
    navigation = root.joinpath("static", "client_context.js").read_text(encoding="utf-8")

    assert "id='reportsView'" in html
    assert "id='cashflowsCard'" in html
    assert "data-report-key='cashflows'" in html
    assert "class='module-card coming-soon'" in html
    assert "/api/reconciliation/reports/preview" not in javascript
    assert 'id="reportsCard"' in dashboard
    assert "reconciliation.html?view=reports" in navigation
    assert 'onclick=' not in html
    assert "details: 'transfers,customer_payments,vendor_payments,deductions,review'" in javascript
    assert "cashflowChildRequest" in javascript
    assert "cashflowsLoadCustomerPaymentsButton" in html
    assert "cashflowsCustomerPaymentsSection" in html
    assert "customer_payments" in javascript
    assert "customer_payment_details" in javascript
    assert "label: 'Receiving account'" in javascript
    assert "label: 'Total deductions'" in javascript
    assert "label: 'Technical bank offset'" in javascript
    assert "label: 'Expected settlement'" in javascript
    assert "label: 'Actual settlement'" in javascript
    assert "label: 'Difference'" in javascript
    assert "cashflows-customer-payments-table" in stylesheet
    assert ".cashflows-customer-payments-table { min-width: 1750px; }" in stylesheet
    assert "formatCashflowTransferParty" in javascript
    assert "renderCashflowTransferReconciliation" in javascript
    assert "cashflowsTransferReconciliation" in html
    assert "prefix + '_account_name'" in javascript
    assert "status_display" in javascript
    assert "setActiveCashflowWindow" in javascript
    assert "const CASHFLOW_DEFAULT_START = '2026-01-01';" in javascript
    assert "const CASHFLOW_DEFAULT_END = '2026-06-30';" in javascript
    assert "<script src='/static/reconciliation.js?v=69'></script>" in html
    assert "id='reconReportPeriod'" in html
    assert "Linked to the loaded Cashflows Report" in html
    assert "id='reconPeriod'" not in html
    assert "Period selection changes this display only" not in javascript
    assert "cashflows-table-number" in javascript
    assert "grid-template-columns: minmax(0, 1fr)" in stylesheet
    assert ".cashflows-table td.cashflows-table-text" in stylesheet
    assert "cashflowsDrilldownExportButton" in html
    assert "downloadCashflowReport('drilldown')" in javascript
    assert "summary_filter" in javascript
    assert "hide_zero_rows" in javascript
    assert "let hideCashflowZeroRows = true;" in javascript
    assert "if (amount === 0) return '-';" in javascript
    assert "minimumFractionDigits: 0" in javascript
    assert "cashflows-summary-section-row" in javascript
    assert "cashflows-summary-total-row" in javascript
    assert "cashflows-summary-category-value" in javascript
    assert "cashflows-summary-total-value" in javascript
    assert "cashflows-summary-local-total-value" in javascript
    assert "Account code: " in javascript
    assert "cashflows-inline-ledger-type" in javascript
    assert "const accountName = rawName.split" in javascript
    assert ".cashflows-inline-ledger-link { display: inline;" in stylesheet
    assert ".cashflows-summary-pane-table tbody tr:hover td { background: #f8fbfd; }" in stylesheet
    assert "const columns = ['Category'].concat(periods.map" in javascript
    assert "const frozenColumnCount = 1" in javascript
    assert "cashflows-summary-sticky-header" in javascript
    assert "syncCashflowSummaryUniversalScrollbar" in javascript
    assert "cashflows-summary-universal-scrollbar-thumb" in javascript
    assert "role', 'scrollbar'" in javascript
    assert "event.key === 'Home'" in javascript
    assert ".cashflows-summary-scroll-pane::-webkit-scrollbar { display: none; }" in stylesheet
    assert ".cashflows-summary-universal-scrollbar-thumb { position: absolute;" in stylesheet
    assert "cashflowSummaryPaneRowParts" in javascript
    assert "cashflows-summary-sticky-header { position: sticky; top: 72.5px;" in stylesheet
    assert ".cashflows-summary-scroll-pane { min-width: 0; overflow-x: auto; overflow-y: clip; }" in stylesheet
    assert "style.css?v=76" in html
    assert "table.style.width = fillAvailable ? 'max(100%, ' + totalWidth + 'px)' : totalWidth + 'px';" in javascript

    assert "color: #7E350E" in stylesheet
    assert "cashflowInlineLedgerHostMarkup(category, section, colspan, true)" in javascript
    assert ".cashflows-summary-total-row th" in stylesheet
    assert "#0070C0" in stylesheet
    assert "#7E350E" in stylesheet
    assert "--cashflows-summary-frozen-width" in stylesheet
    assert "overflow-wrap: anywhere" in stylesheet
    assert "position: static !important" in stylesheet
    assert ".cashflows-summary-total-row th, .cashflows-summary-total-row td { background: #e8eef7;" in stylesheet
    load_summary_index = html.index("<button type='submit' class='cashflows-primary'>Load summary</button>")
    drilldown_index = html.index("id='cashflowsLedgerDrilldownButton'")
    actions_index = html.index("class='cashflows-report-actions'")
    hide_zero_index = html.index("id='cashflowsHideZeroRowsButton'")
    export_index = html.index("id='cashflowsExportButton'")
    assert load_summary_index < actions_index < drilldown_index < hide_zero_index < export_index

    assert "cashflowsDrilldownShortcuts" in html
    assert "cashflowsDrilldownLoadTransfersButton" in html
    assert "cashflowsDrilldownLoadCustomerPaymentsButton" in html
    assert "cashflowsDrilldownLoadDeductionsButton" in html
    assert "cashflowsDrilldownLoadReviewButton" in html
    assert "cashflowsDrilldownViewTransactionsButton" in html
    assert "data-cashflow-child-shortcut" in html
    assert "cashflowChildShortcutTargets" in javascript
    assert "syncCashflowDrilldownShortcuts" in javascript
    assert javascript.index("function syncCashflowDrilldownShortcuts") < javascript.index("function resetLazyCashflowSections")
    assert "cashflowsDetailResults" in html
    assert "cashflowsMainDetailAnchor" in html
    assert "cashflowsDrilldownDetailHost" in html
    assert "function moveCashflowDetailResults" in javascript
    assert "moveCashflowDetailResults(open)" in javascript
    ledger_table_index = html.index("id='cashflowsLedgerDrilldownTable'")
    ledger_pagination_index = html.index("id='cashflowsDrilldownPrevious'")
    shortcut_index = html.index("id='cashflowsDrilldownShortcuts'")
    detail_host_index = html.index("id='cashflowsDrilldownDetailHost'")
    assert ledger_table_index < ledger_pagination_index < shortcut_index < detail_host_index
    assert ".cashflows-drilldown-shortcuts" in stylesheet
    for search_id in (
        "cashflowsTransfersSearch",
        "cashflowsCustomerPaymentsSearch",
        "cashflowsDeductionsSearch",
        "cashflowsReviewSearch",
    ):
        assert search_id in html
    assert html.count("Search any value in any column") == 4
    assert "Search any visible column" in html
    assert "cashflowDetailSearchInputs" in javascript
    assert "filterCashflowDetailRows" in javascript
    assert "formatCashflowDetailCount" in javascript
    assert "cashflows-detail-search-toolbar" in stylesheet
    assert ".cashflows-detail-search-input" in stylesheet


def test_financial_statement_summary_aggregates_central_categories_and_flags_unmapped():
    journal = pd.DataFrame([
        {"voucher_id": "V1", "date": "2026-01-10", "account_code": "EXP", "account_name": "Expense", "debit": 100, "credit": 0},
        {"voucher_id": "V1", "date": "2026-01-10", "account_code": "CASH", "account_name": "Cash", "debit": 0, "credit": 100},
        {"voucher_id": "V2", "date": "2026-02-05", "account_code": "UNMAPPED", "account_name": "Unmapped", "debit": 25, "credit": 0},
    ])
    coa = pd.DataFrame([
        {"account_code": "EXP", "account_title": "Expense", "account_type": "Expense", "category": "Operating expense"},
        {"account_code": "CASH", "account_title": "Cash", "account_type": "Cash", "category": "Cash and bank"},
    ])

    result = build_financial_statement_summary(journal, coa, "2026-01-01", "2026-02-28")

    assert result["months"] == ["2026-01", "2026-02"]
    totals = result["category_totals"]
    assert totals.loc["Operating expense", "2026-01"] == 100
    assert totals.loc["Cash and bank", "2026-01"] == -100
    assert result["unmapped_account_count"] == 1
    assert result["unmapped_accounts"].iloc[0]["account_code"] == "UNMAPPED"


def test_financial_statement_preview_uses_account_control_mapping_source():
    summary = {
        "months": ["2026-01"],
        "category_totals": pd.DataFrame({"2026-01": [125.0], "Total": [125.0]}, index=["Operating expense"]),
        "account_counts": pd.Series({"Operating expense": 2}),
        "unmapped_accounts": pd.DataFrame(columns=["account_code", "account_name", "debit", "credit", "net_movement"]),
        "selected_voucher_count": 3,
        "source_line_count": 6,
        "mapped_account_count": 2,
        "unmapped_account_count": 0,
    }
    with patch.object(reconciliation, "_require_active_client"), \
         patch.object(reconciliation, "load_ledger_report_source", return_value=_source()) as loader, \
         patch.object(reconciliation, "build_financial_statement_summary", return_value=summary):
        response = reconciliation.preview_financial_statements(
            7, "2026-01-01", "2026-01-31", journal_upload_id=21, journal_template_id=None, session=None
        )

    assert response["controls"]["financial_statement_mapping"]["status"] == "Ready"
    assert response["preview"]["category_totals"][0]["category"] == "Operating expense"
    assert response["preview"]["account_counts"] == [{"category": "Operating expense", "account_count": 2}]
    loader.assert_called_once()
    assert loader.call_args.kwargs["account_category_source"] == "financial_statement"


def test_reports_hub_exposes_requested_report_cards():
    root = Path(__file__).resolve().parents[1]
    html = root.joinpath("static", "reconciliation.html").read_text(encoding="utf-8")
    javascript = root.joinpath("static", "reconciliation.js").read_text(encoding="utf-8")

    assert "Cashflows" in html
    assert "Vendor Reports" in html
    assert "Customer Reports" in html
    assert "Financial Statements" in html
    assert "Draft disclosures — before audit adjustments" not in html
    assert "balanceSheetNotesPeriod" in html
    assert "incomeStatementNotesPeriod" in html
    assert "bs-note-subtotal" in javascript
    assert "bs-note-group-gap" in javascript
    assert "balanceSheetUpload" in html
    assert "/api/reconciliation/financial-statements/balance-sheet" in javascript
    assert "/api/reconciliation/financial-statements/income-statement" in javascript
    assert "data-report-key='income-statement'" in html
    assert "id='balanceSheetReconciliation'" in html
    assert "id='statementReconciliationRows'" in html
    assert "renderStatementReconciliation(result.transfer_reconciliation)" in javascript
    assert "<h1>Reports centre</h1>" not in html
    assert "reportsHubMessage" not in html
    assert "id='previewFinancialStatementsButton'" not in html
    assert "id='financialStatementPreviewPanel'" not in html
    assert "/api/reconciliation/financial-statements/preview?" not in javascript

def test_cashflows_preview_force_refresh_bypasses_cached_snapshots_and_reports_freshness():
    configuration = {
        "source_template_id": 9,
        "cash_account_codes": [],
        "bank_account_codes": ["B1"],
        "match_internal_transfers": False,
        "receipt_presentation": "net",
        "cashflow_report_revision": 7,
    }
    with (
        patch.object(reconciliation, "_require_active_client"),
        patch.object(reconciliation, "_cashflow_configuration", return_value=configuration),
        patch.object(reconciliation, "load_cashflow_summary_read_model") as read_model,
        patch.object(reconciliation, "load_cashflow_prepared_summary") as prepared,
        patch.object(reconciliation, "load_ledger_report_source", return_value=_source()) as loader,
        patch.object(reconciliation, "build_cashflow_summary", return_value=_cashflow_summary()),
        patch.object(reconciliation, "store_cashflow_summary_read_model"),
    ):
        response = reconciliation.preview_cashflows(
            7,
            "2026-02-10",
            "2026-04-18",
            group_by="quarter",
            details="summary",
            refresh_from_source=True,
            session=None,
        )

    read_model.assert_not_called()
    prepared.assert_not_called()
    loader.assert_called_once()
    assert response["freshness"]["mode"] == "client_data_source"
    assert response["freshness"]["authoritative"] is True
    assert response["freshness"]["report_revision"] == 7


def test_cashflow_revision_endpoint_returns_lightweight_revision_and_source_key():
    with (
        patch.object(reconciliation, "_require_active_client"),
        patch.object(reconciliation, "_cashflow_configuration", return_value={"source_template_id": 9, "cashflow_report_revision": 12}),
    ):
        response = reconciliation.cashflow_revision(7, session=None)

    assert response == {
        "client_id": 7,
        "cashflow_report_revision": 12,
        "cashflow_report_source_key": "client-data-v1:7:9",
        "cashflow_configuration_key": reconciliation._cashflow_configuration_key({"source_template_id": 9, "cashflow_report_revision": 12}),
    }


@pytest.mark.parametrize('view', ['combined', 'bank', 'cash', 'split'])
@pytest.mark.parametrize('presentation', ['net', 'gross'])
def test_warm_ledger_preview_uses_one_source_load_and_preserves_accounting(view, presentation):
    configuration = dict(source_template_id=9, cash_account_codes=[], bank_account_codes=['B1'],
                         match_internal_transfers=False, receipt_presentation=presentation, cashflow_report_revision=7)
    journal = pd.DataFrame([
        dict(voucher_id='V1', date='2026-01-05', account_code='B1', account_name='Bank', debit=125.25, credit=0),
        dict(voucher_id='V1', date='2026-01-05', account_code='AR', account_name='Customer', debit=0, credit=125.25),
    ])
    journal['voucher_detail'] = 'Receipt'
    coa = pd.DataFrame([
        dict(account_code='B1', account_title='Bank', account_type='Bank', category=''),
        dict(account_code='AR', account_title='Customer', account_type='Customer', category='Customers'),
    ])
    source = SimpleNamespace(journal=journal, coa=coa, diagnostics=(), ready=True,
                             metadata={'cashflow_report_source_key': 'client-data-v1:7:9'})
    with (
        patch.object(reconciliation, '_require_active_client'),
        patch.object(reconciliation, '_cashflow_configuration', return_value=configuration),
        patch.object(reconciliation, 'load_ledger_report_source', return_value=source) as loader,
        patch.object(reconciliation, 'build_cashflow_summary', wraps=build_cashflow_summary) as builder,
    ):
        cold = reconciliation.preview_cashflows(7, '2026-01-01', '2026-01-31',
            details='summary', group_by='month', treasury_view=view, warm_ledger_cache=False, session=None)
        loader.reset_mock()
        builder.reset_mock()
        warm = reconciliation.preview_cashflows(7, '2026-01-01', '2026-01-31',
            details='summary', group_by='month', treasury_view=view, warm_ledger_cache=True, session=None)
    assert loader.call_count == builder.call_count == 1
    assert builder.call_args.kwargs['include_drilldown'] is True
    descriptor = warm['preview']['ledger_cache']
    assert descriptor['complete'] is True
    assert descriptor['row_count'] == 1
    assert descriptor['rows'][0]['amount'] == 125.25
    assert descriptor['context'] == warm['ledger_context']
    assert descriptor['context']['template_id'] == 9
    assert warm['preview']['category_totals'] == cold['preview']['category_totals']
    assert warm['preview']['treasury_category_totals'] == cold['preview']['treasury_category_totals']
    assert warm['preview']['treasury_reconciliation'] == cold['preview']['treasury_reconciliation']
    assert warm['preview']['ledger_rollups'] == []
    # Export calculations remain independent of this disposable transport/cache.
    warm_without_cache = dict(warm['preview'])
    warm_without_cache.pop('ledger_cache')
    assert warm_without_cache == cold['preview']


def test_browser_rollup_budget_is_all_or_nothing(monkeypatch):
    rows = [dict(category='Operating', amount=1, account_name='账簿') for _ in range(3)]
    monkeypatch.setattr(reconciliation, 'CASHFLOW_BROWSER_LEDGER_MAX_ROWS', 2)
    result = reconciliation._cashflow_browser_ledger_payload(rows, {'client_id': 7})
    assert result == dict(context={'client_id': 7}, complete=False, row_count=3, rows=[])
    monkeypatch.setattr(reconciliation, 'CASHFLOW_BROWSER_LEDGER_MAX_ROWS', 3)
    monkeypatch.setattr(reconciliation, 'CASHFLOW_BROWSER_LEDGER_MAX_BYTES', 10)
    assert reconciliation._cashflow_browser_ledger_payload(rows, {})['complete'] is False
    monkeypatch.setattr(reconciliation, 'CASHFLOW_BROWSER_LEDGER_MAX_BYTES', 2048)
    assert reconciliation._cashflow_browser_ledger_payload(rows, {})['rows'] == rows
    assert reconciliation._cashflow_browser_ledger_payload([], {})['complete'] is True


def test_oversized_drilldown_omits_baseline_but_keeps_all_pages():
    rows = [dict(category='Operating', account_code=f'A{i}', amount=i) for i in range(5001)]
    result = dict(period_start='2026-01-01', period_end='2026-01-31', group_by='month',
                  treasury_view='combined', preview={'ledger_rollups': rows})
    with patch.object(reconciliation, '_build_cashflow_result', return_value=result):
        response = reconciliation.cashflow_drilldown(7, '2026-01-01', '2026-01-31',
            group_by='month', search='', category='', account_head='', treasury_type='', period_key='',
            treasury_view='combined', page=26, limit=200, session=None)
    assert response['base_rows'] is None
    assert response['total_rows'] == 5001
    assert response['page_count'] == 26
    assert response['rows'] == rows[5000:]


def test_configuration_identity_includes_categories_treasury_and_receipt_rules():
    base = dict(source_template_id=9, cash_account_codes=['C1'], receipt_presentation='net',
                category_mappings={'AR': 'Customers'})
    key = reconciliation._cashflow_configuration_key(base)
    for change in ({'category_mappings': {'AR': 'Other'}}, {'cash_account_codes': ['C2']},
                   {'receipt_presentation': 'gross'}, {'source_template_id': 10}):
        assert reconciliation._cashflow_configuration_key(base | change) != key


def test_disposable_ledger_cache_does_not_change_export_cells_or_formats():
    summary = _cashflow_summary()
    summary['cashflow_ledger_rollups'] = pd.DataFrame([
        dict(category='Customers', period_key=key, period_label=key, account_code='AR',
             account_name='Customers', treasury_type='Bank', amount=amount, line_count=1)
        for key, amount in [('2026-Q1', 125), ('2026-Q2', 50)]
    ])
    preview = reconciliation._cashflow_preview(summary, {'drilldown'})
    cached = preview | {'ledger_cache': reconciliation._cashflow_browser_ledger_payload(
        preview['ledger_rollups'], {'client_id': 7})}
    workbooks = [load_workbook(BytesIO(export_cashflow_xlsx(payload,
        period_start='2026-02-10', period_end='2026-04-18', group_by='quarter')))
        for payload in (preview, cached)]
    assert workbooks[0].sheetnames == workbooks[1].sheetnames
    for name in workbooks[0].sheetnames:
        cells = [list((cell.coordinate, cell.value, cell.number_format)
                      for row in workbook[name] for cell in row) for workbook in workbooks]
        assert cells[0] == cells[1]


def test_signed_party_preview_is_complete_and_vendor_identity_changes_context():
    summary = _cashflow_summary()
    summary['customer_payment_details'] = pd.DataFrame([{'voucher_id': f'V{i}'} for i in range(251)])
    summary['vendor_payment_details'] = pd.DataFrame([{'voucher_id': f'P{i}'} for i in range(260)])
    preview = reconciliation._cashflow_preview(summary, {'customer_payments', 'vendor_payments'})
    assert len(preview['customer_payment_details']) == 251
    assert len(preview['vendor_payment_details']) == 260
    assert reconciliation._cashflow_configuration_key({'vendor_account_codes': ['AP1']}) != reconciliation._cashflow_configuration_key({'vendor_account_codes': ['AP2']})
    assert 'vendor_payments' in reconciliation._parse_cashflow_detail_sections('vendor_payments')


def test_all_deduction_details_survive_preview_and_excel_export():
    summary = _cashflow_summary()
    summary['deduction_details'] = pd.DataFrame([
        dict(period_key='2026-Q1', period_label='Q1 2026', category='Charges',
             account_name=f'Charge {index}', amount=-(index + 0.25), presentation='shown_separately')
        for index in range(932)
    ])
    preview = reconciliation._cashflow_preview(summary, {'deductions'})
    assert preview['deduction_count'] == len(preview['deduction_details']) == 932
    assert preview['deduction_details'][-1]['account_name'] == 'Charge 931'
    workbook = load_workbook(BytesIO(export_cashflow_xlsx(preview)), data_only=True)
    sheet = workbook['Customer Deductions']
    assert sheet.max_row - 4 == 932
    assert sheet.cell(936, 3).value == 'Charge 931'
    assert sum(sheet.cell(row, 4).value for row in range(5, 937)) == sum(summary['deduction_details']['amount'])
    assert reconciliation._cashflow_preview(summary, set())['deduction_details'] == []


def test_balance_proof_never_falls_back_to_categories_when_source_proof_is_missing():
    configuration = dict(source_template_id=9, cash_account_codes=['C1'], bank_account_codes=['B1'],
                         match_internal_transfers=True, receipt_presentation='net')
    with patch.object(reconciliation, '_require_active_client'), \
            patch.object(reconciliation, '_cashflow_configuration', return_value=configuration), \
            patch.object(reconciliation, '_latest_trial_balance_accounts', return_value=[]), \
            patch.object(reconciliation, 'load_ledger_report_source', return_value=_source()), \
            patch.object(reconciliation, 'build_cashflow_summary', return_value=_cashflow_summary()), \
            patch.object(reconciliation, 'load_uploaded_ledger_balances', return_value={}) as balances:
        result = reconciliation._build_cashflow_result(7, '2026-02-10', '2026-04-18', include_balances=True)
    assert all(value is None for value in balances.call_args.args[3].values())
    assert all(value is None for value in result['preview']['balance_reconciliation']['unresolved_movements'].values())


def test_treasury_proof_roundtrip_and_daily_aggregation_keep_actual_source_periods():
    from services.ledger_report_source import _cashflow_summary_payload, _cashflow_summary_from_payload, _cashflow_daily_rollup
    journal = pd.DataFrame([
        ['T', '2026-01-01', 'B', 0, 100], ['T', '2026-01-01', 'C', 100, 0],
        ['M', '2026-01-02', 'B', 0, 105], ['M', '2026-01-02', 'C', 100, 0], ['M', '2026-01-02', 'E', 5, 0],
    ], columns=['voucher_id', 'date', 'account_code', 'debit', 'credit'])
    coa = pd.DataFrame([dict(account_code=code, account_title=code, category='Charges' if code == 'E' else '') for code in ['B', 'C', 'E']])
    summary = build_cashflow_summary(journal, coa, ['C'], ['B'], '2026-01-01', '2026-01-02', group_by='day')
    payload = _cashflow_summary_payload(summary)
    assert _cashflow_summary_from_payload(payload, 'day')['treasury_movement_proof'] == summary['treasury_movement_proof']
    rolled = _cashflow_daily_rollup(payload, '2026-01-01', '2026-01-02', 'month')
    proof = {r['treasury_type']: r for r in rolled['treasury_movement_proof']}
    assert proof['bank']['source_movement'] == -205
    assert proof['cash']['source_movement'] == 200
    assert proof['combined']['source_movement'] == -5
    assert rolled['treasury_reconciliation']['status'] == 'Review required'
    subset = _cashflow_daily_rollup(payload, '2026-01-02', '2026-01-02', 'month')
    assert next(r for r in subset['treasury_movement_proof'] if r['treasury_type'] == 'bank')['internal_transfer_movement'] == 0
    summary['transfer_details'] = pd.DataFrame([{'voucher_id': f'T{i}'} for i in range(251)])
    assert len(reconciliation._cashflow_preview(summary, {'transfers'})['transfer_details']) == 251
