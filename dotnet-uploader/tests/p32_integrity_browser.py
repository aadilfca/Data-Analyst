"""Owned hosted-page coverage; all source data and sessions are synthetic."""
import json
import sys
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import Error, expect, sync_playwright

directory = Path(sys.argv[1]).resolve()
fixture = json.loads((directory / 'fixture.json').read_text(encoding='utf-8'))
address = fixture['address']
assert urlparse(address).hostname == '127.0.0.1'
assert directory.parent.name == '.tools' and directory.name.startswith('p32-')
disabled = len(sys.argv) > 2 and sys.argv[2] == 'disabled'

with sync_playwright() as playwright:
    browser = playwright.chromium.launch()
    context = browser.new_context(viewport={'width': 1280, 'height': 900})
    context.add_cookies([{'name': 'session_token', 'value': 'a' * 64, 'url': address}])
    page = context.new_page()
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    requests = []
    page.on('request', lambda request: requests.append(request) if '/api/data-integrity/' in request.url else None)
    page.goto(address + '/static/data_integrity.html')
    page.wait_for_function('window.AppNativeDataIntegrity !== undefined')
    if disabled:
        page.get_by_text('Native Data Integrity is disabled.', exact=True).first.wait_for()
        assert page.locator('#refreshIntegrityBtn').is_disabled()
        assert page.locator('#safeguardsOverallStatus').inner_text() == 'Not checked'
        assert not requests
        page.screenshot(path=str(directory / 'disabled.png'), full_page=True)
    else:
        page.wait_for_function("document.querySelector('#safeguardsOverallStatus').textContent === 'Needs attention'")
        assert requests and requests[0].headers.get('x-app-context') == fixture['context']
        assert page.locator('#safeguardsTab').get_attribute('aria-selected') == 'true'
        assert 'Alpha <safe>' in page.locator('#integrityClientLabel').inner_text()
        assert page.locator('#integrityClientLabel safe').count() == 0
        page.locator('#inventoryTab').click()
        assert 'module-inventory' in page.url
        assert page.locator('#integrityTableBody tr').count() == 3
        assert '10,005' in page.locator('#integrityTableBody tr').first.inner_text()
        assert '2 invalid upload links' in page.locator('#integrityTableBody').inner_text()
        assert 'foreign-secret' not in page.locator('main').inner_text()
        # A failed hard refresh clears all previously successful evidence.
        page.route('**/api/data-integrity/overview?*', lambda route: route.fulfill(status=403, json={'code': 'denied'}))
        page.locator('#refreshIntegrityBtn').click()
        page.wait_for_function("document.querySelector('#safeguardsOverallStatus').textContent === 'Not checked'")
        assert 'Data check unavailable' in page.locator('#integrityTableBody').inner_text()
        page.unroute('**/api/data-integrity/overview?*')
        page.locator('#refreshIntegrityBtn').click()
        page.wait_for_function("document.querySelector('#safeguardsOverallStatus').textContent === 'Needs attention'")
        # Inconsistent response scope cannot restore another client's Passed result.
        other = json.loads((directory / 'clean.json').read_text(encoding='utf-8'))
        other['client_id'] = 20
        page.route('**/api/data-integrity/overview?*', lambda route: route.fulfill(json=other))
        page.locator('#refreshIntegrityBtn').click()
        page.wait_for_function("document.querySelector('#safeguardsOverallStatus').textContent === 'Not checked'")
        assert page.locator('#safeguardsModules').get_by_text('Passed', exact=True).count() == 0
        page.unroute('**/api/data-integrity/overview?*')
        # A delayed response after hiding the document stays discarded.
        delayed = []
        page.route('**/api/data-integrity/overview?*', lambda route: delayed.append(route))
        page.locator('#refreshIntegrityBtn').click()
        for _ in range(100):
            if delayed:
                break
            page.wait_for_timeout(20)
        assert delayed
        page.evaluate("""() => { Object.defineProperty(document,'visibilityState',{configurable:true,value:'hidden'});
            document.dispatchEvent(new Event('visibilitychange')); }""")
        try:
            delayed[0].fulfill(json=json.loads((directory / 'clean.json').read_text(encoding='utf-8')))
        except Error:
            pass  # An aborted transport may already have discarded its route.
        assert page.locator('#safeguardsOverallStatus').inner_text() == 'Not checked'
        page.unroute('**/api/data-integrity/overview?*')
        page.evaluate("""() => { Object.defineProperty(document,'visibilityState',{configurable:true,value:'visible'});
            document.dispatchEvent(new Event('visibilitychange')); }""")
        page.wait_for_function("document.querySelector('#safeguardsOverallStatus').textContent === 'Needs attention'")
        expect(page.locator('#apiRetryToast')).not_to_have_class('api-retry-toast show')
        for width in (320, 390, 768):
            page.set_viewport_size({'width': width, 'height': 900})
            page.locator('#safeguardsTab').click()
            page.locator('#safeguardsTab').press('ArrowRight')
            assert page.locator('#inventoryTab').get_attribute('aria-selected') == 'true'
            # The existing wide shared navbar is outside P32. The integrity
            # workspace itself must fit, with its table scrolling internally.
            box = page.locator('main').bounding_box()
            assert box['x'] >= 0 and box['x'] + box['width'] <= width + 1
            assert page.locator('.integrity-table-wrap').evaluate('(node) => node.scrollWidth >= node.clientWidth')
            page.screenshot(path=str(directory / ('inventory-' + str(width) + '.png')), full_page=True)
        page.set_viewport_size({'width': 1280, 'height': 900})
        page.locator('#safeguardsTab').click()
        page.screenshot(path=str(directory / 'safeguards.png'), full_page=True)
        # Navigation/search reuse the registered deep links.
        assert page.evaluate("typeof NAV_MANIFEST !== 'undefined' && JSON.stringify(NAV_MANIFEST).includes('module-inventory')")
    assert not errors, errors
    (directory / ('browser-disabled.json' if disabled else 'browser-result.json')).write_text(
        json.dumps({'passed': True, 'requests': len(requests), 'errors': errors}), encoding='utf-8')
    browser.close()
print('P32 native browser passed' + (' (disabled, no feature calls).' if disabled else ' (scope, refresh, failure, visibility, tabs and mobile).'))
