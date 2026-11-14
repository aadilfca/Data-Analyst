"""Real retained review page against an owned native host; no live data."""
import json
import sys
from pathlib import Path
from urllib.parse import urlparse, parse_qs
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
directory = Path(sys.argv[1]).resolve()
assert directory.is_relative_to(ROOT / '.tools') and directory.name.startswith('p36-')
fixture = json.loads((directory / 'browser.json').read_text()); address = fixture['address']
assert urlparse(address).hostname == '127.0.0.1'
with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    context = browser.new_context(); context.add_cookies([{'name': 'session_token', 'value': 'a' * 64, 'url': address}])
    page = context.new_page(); errors = []; page.on('pageerror', lambda error: errors.append(str(error)))
    page.goto(address + f"/static/trial_balance.html?view=available&upload_id={fixture['upload']}")
    if len(sys.argv) > 2 and sys.argv[2] == 'scale':
        page.wait_for_function("typeof tbNative !== 'undefined' && tbNative.data?.pagination.total===10000")
        baseline = page.locator('.tb-draft-adjusted_totals').inner_text()
        page.locator('#tbNativeNext').click(); page.wait_for_function('tbNative.data.pagination.page===2')
        assert page.locator('.tb-draft-adjusted_totals').inner_text() == baseline
        held = []
        def hold_old(route):
            if parse_qs(urlparse(route.request.url).query).get('search') == ['Account 9']:
                held.append((route, route.fetch()))
            else: route.continue_()
        page.route('**/api/trial-balance/accounts?**', hold_old)
        page.locator('#tbNativeSearch').fill('Account 9'); page.locator('#tbNativeFind').click()
        for _ in range(100):
            if held: break
            page.wait_for_timeout(50)
        assert held
        page.locator('#tbNativeSearch').fill('Account 9999'); page.locator('#tbNativeFind').click()
        page.wait_for_function("tbNative.data.pagination.search==='Account 9999' && tbNative.data.accounts.length===1")
        with page.expect_response(lambda response: response.url == held[0][0].request.url): held[0][0].fulfill(response=held[0][1])
        page.wait_for_timeout(100)
        assert page.evaluate('tbNative.data.pagination.search') == 'Account 9999'
        assert page.locator('.tb-draft-adjusted_totals').inner_text() == baseline
        assert not errors, errors
        page.screenshot(path=str(directory / 'native-scale.png'), full_page=True)
        (directory / 'browser-scale-result.json').write_text(json.dumps({'passed': True, 'accounts': 10000, 'whole_upload_footings': True, 'late_response_rejected': True}))
        context.close(); browser.close()
        print('P36 scale browser passed')
        sys.exit(0)
    page.wait_for_function("typeof tbNative !== 'undefined' && tbNative.data?.accounts.length===3")
    assert page.locator('#uploadViewContainer').is_hidden()
    assert page.locator('#tbNativeReviewTools').is_visible()
    assert 'Pending' not in page.locator('#availableAccountsTableBody').inner_text()
    assert page.locator('#closeAvailableTrialBalancesBtn').is_hidden()
    row = page.locator('tr[data-account-code="001"]')
    row.locator('.tb-account-code-display').dblclick()
    edit = row.locator('input.tb-code-review-input'); edit.fill('010'); edit.press('Enter')
    assert page.evaluate('tbPendingCodeCorrections.size') == 1
    page.locator('#tbSaveCodeCorrections').click()
    page.wait_for_function("!tbCodeSaveRunning && tbPendingCodeCorrections.size===0 && tbNative.data?.accounts[0].account_code==='010'")
    assert page.locator('tr[data-account-code="010"] .tb-account-code-display').inner_text() == '010'
    assert 'out of date' in page.locator('#tbSavedValidationControls').inner_text()
    row = page.locator('tr[data-account-code="010"]'); cell = row.locator('td').nth(2); cell.dblclick()
    cell.locator('select').select_option('parent'); cell.locator('select').press('Enter')
    page.wait_for_function("tbNative.data?.accounts[0].manually_overridden && !tbNative.data.accounts[0].is_leaf")
    assert 'Pending' not in page.locator('#availableAccountsTableBody').inner_text()
    # A definite save rejection retains the draft and source figures.
    row.locator('.tb-account-code-display').dblclick(); edit = row.locator('input.tb-code-review-input'); edit.fill('001-01'); edit.press('Enter')
    page.locator('#tbSaveCodeCorrections').click(); page.wait_for_function('!tbCodeSaveRunning')
    assert page.evaluate('tbPendingCodeCorrections.size') == 1
    page.locator('#tbDiscardCodeCorrections').click()
    # Drop only the response, after the actual native transaction commits.
    def lose_ack(route):
        route.fetch()
        route.abort('failed')
    page.route('**/review/codes?**', lose_ack)
    row = page.locator('tr[data-account-code="010"]')
    row.locator('.tb-account-code-display').dblclick(); edit = row.locator('input.tb-code-review-input'); edit.fill('099'); edit.press('Enter')
    page.locator('#tbSaveCodeCorrections').click()
    page.wait_for_function('!tbCodeSaveRunning && tbNative.pending!==null')
    assert page.evaluate('tbPendingCodeCorrections.size') == 1
    page.unroute('**/review/codes?**', lose_ack)
    page.locator('#tbNativeRecover').click()
    page.wait_for_function("tbNative.pending===null && tbNative.data.accounts[0].account_code==='099'")
    assert page.evaluate('tbPendingCodeCorrections.size') == 0
    page.locator('#tbNativeSearch').fill('literal'); page.locator('#tbNativeFind').click()
    page.wait_for_function("tbNative.data?.pagination.search==='literal' && tbNative.data.accounts.length===1")
    assert page.evaluate('tbNative.data.accounts[0].closing_dr') == 13
    page.locator('#tbNativeReviewTools details summary').click()
    assert 'read only' in page.locator('#tbNativeReviewTools details').inner_text()
    page.screenshot(path=str(directory / 'native-review.png'), full_page=True)
    assert not errors, errors
    (directory / 'browser-result.json').write_text(json.dumps({'passed': True, 'native_review': True, 'draft_retained_after_rejection': True, 'lost_response_recovered': True, 'page_errors': errors}))
    context.close(); browser.close()
print('P36 native review browser passed')
