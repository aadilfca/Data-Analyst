"""P33 hosted native UI; synthetic owned fixtures only, no live cleanup targets."""
import json
import sys
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import Error, expect, sync_playwright

directory = Path(sys.argv[1]).resolve()
fixture = json.loads((directory / 'fixture.json').read_text(encoding='utf-8'))
address = fixture['address']
assert directory.parent.name == '.tools' and directory.name.startswith('p33-')
assert urlparse(address).hostname == '127.0.0.1'
disabled = len(sys.argv) > 2 and sys.argv[2] == 'disabled'
phrase = 'PERMANENTLY DELETE SELECTED DATA'

with sync_playwright() as playwright:
    browser = playwright.chromium.launch()
    context = browser.new_context(viewport={'width': 1280, 'height': 900}, accept_downloads=True)
    context.add_cookies([{'name': 'session_token', 'value': 'a' * 64, 'url': address}])
    page = context.new_page()
    errors, cleanup_requests, purge_posts = [], [], []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.on('request', lambda request: cleanup_requests.append(request) if '/api/storage-cleanup/' in request.url else None)
    page.on('request', lambda request: purge_posts.append(request) if '/api/storage-cleanup/apply?' in request.url and 'kind=purge' in request.url and request.method == 'POST' else None)
    page.goto(address + '/static/storage_cleanup.html')
    page.locator('#nativeCleanupPanel').wait_for(state='visible')
    if disabled:
        expect(page.locator('#nativeCleanupStatus')).to_contain_text('disabled')
        assert not cleanup_requests
        assert page.locator('#nativeCleanupApply').is_disabled()
        page.goto(address + '/static/databank.html')
        page.wait_for_function("typeof nativeDataBank !== 'undefined'")
        assert page.get_by_role('button', name='Back up and delete data for Browser scope', exact=True).count() == 0
        page.screenshot(path=str(directory / 'disabled.png'), full_page=True)
    else:
        page.locator('#nativeCleanupTemplate option').first.wait_for(state='attached')
        page.locator('#nativeCleanupTemplate').select_option(label='Browser scope')
        expect(page.locator('#nativeCleanupCounts')).to_contain_text('1 temporary plans', timeout=30000)
        page.locator('#nativeCleanupConfirmation').fill('DELETE')
        assert page.locator('#nativeCleanupApply').is_disabled()
        page.locator('#nativeCleanupConfirmation').fill('DELETE APPROVED VALIDATION PLANS')
        page.locator('#nativeCleanupApply').click()
        expect(page.locator('#nativeCleanupStatus')).to_contain_text('committed', timeout=30000)
        page.locator('#nativeCleanupRefresh').click()
        expect(page.locator('#nativeCleanupCounts')).to_contain_text('0 temporary plans', timeout=30000)
        assert not page.locator('#applyCleanupBtn').is_visible()
        assert not page.locator('#indexMaintenanceConfirmation').is_visible()
        for width in (320, 390, 768):
            page.set_viewport_size({'width': width, 'height': 900})
            box = page.locator('#nativeCleanupPanel').bounding_box()
            assert box['x'] >= 0 and box['x'] + box['width'] <= width + 1
            page.screenshot(path=str(directory / f'cleanup-{width}.png'), full_page=True)
        page.set_viewport_size({'width': 1280, 'height': 900})
        page.goto(address + '/static/databank.html')
        remove = page.get_by_role('button', name='Back up and delete data for Browser scope', exact=True)
        remove.wait_for()

        def open_delete():
            remove.click()
            expect(page.locator('#templateDeleteModal')).to_be_visible()
            assert page.locator('#backupAndDeleteTemplateBtn').is_disabled()
            page.locator('#nativePurgeConfirmation').fill(phrase)
            expect(page.locator('#backupAndDeleteTemplateBtn')).to_be_enabled()

        open_delete()
        page.locator('#cancelTemplateDeleteBtn').click()
        assert not purge_posts
        # A failed complete backup cannot submit purge.
        page.route('**/api/data/template/2/backup?*', lambda route: route.fulfill(status=503, json={'code': 'synthetic_failure'}))
        open_delete()
        page.locator('#backupAndDeleteTemplateBtn').click()
        expect(page.locator('#templateDeleteStatus')).to_contain_text('No deletion was submitted', timeout=30000)
        assert not purge_posts
        page.unroute('**/api/data/template/2/backup?*')
        page.locator('#cancelTemplateDeleteBtn').click()
        # A valid but delayed backup is cancelled before any mutation.
        delayed = []
        def delay_backup(route):
            delayed.append((route, route.fetch()))
        page.route('**/api/data/template/2/backup?*', delay_backup)
        open_delete()
        page.locator('#backupAndDeleteTemplateBtn').click()
        for _ in range(100):
            if delayed:
                break
            page.wait_for_timeout(20)
        assert delayed
        page.locator('#cancelTemplateDeleteBtn').click()
        try:
            delayed[0][0].fulfill(response=delayed[0][1])
        except Error:
            pass
        page.unroute('**/api/data/template/2/backup?*')
        expect(page.locator('#templateDeleteModal')).not_to_be_visible()
        page.wait_for_function('nativePurgeController === null && deleteTemplateInFlight.size === 0')
        assert not purge_posts
        # Server commits, but its response is lost: reconcile by immutable ID.
        def lose_response(route):
            response = route.fetch()
            assert response.status == 200, response.text()
            route.abort('failed')
        page.route('**/api/storage-cleanup/apply?*kind=purge*', lose_response)
        open_delete()
        with page.expect_download(timeout=30000) as download:
            page.locator('#backupAndDeleteTemplateBtn').click()
            page.evaluate('void runNativeTemplatePurge()')  # Chokepoint double-submit guard.
        download.value.save_as(directory / 'browser-backup.xlsx')
        expect(page.locator('#nativePurgeRecovery')).to_contain_text('not confirmed', timeout=30000)
        assert len(purge_posts) == 1
        page.unroute('**/api/storage-cleanup/apply?*kind=purge*')
        page.locator('#nativePurgeRecovery').get_by_role('button', name='Check recorded outcome').click()
        expect(page.locator('#nativePurgeRecovery')).to_contain_text('Stored data deleted', timeout=30000)
        expect(page.locator('#nativePurgeRecovery')).to_contain_text('pending')
        page.reload()
        expect(page.locator('#nativePurgeRecovery')).to_contain_text('Stored data deleted', timeout=30000)
        page.wait_for_function('nativePurgeRecoveryBusy === false')
        expect(page.locator('#clientDataLoadingOverlay')).not_to_be_visible(timeout=30000)
        assert len(purge_posts) == 1
        assert purge_posts[0].headers.get('x-app-context') == fixture['context']
        page.screenshot(path=str(directory / 'recovered.png'), full_page=True)
        staff = browser.new_context(viewport={'width': 1280, 'height': 900})
        staff.add_cookies([{'name': 'session_token', 'value': 'c' * 64, 'url': address}])
        staff_page = staff.new_page()
        staff_page.goto(address + '/static/databank.html')
        staff_page.wait_for_function("typeof nativeDataBank !== 'undefined'")
        assert staff_page.get_by_role('button', name='Back up and delete data for Fixture', exact=True).count() == 0
        staff.close()
    assert not errors, errors
    (directory / ('browser-disabled.json' if disabled else 'browser-result.json')).write_text(
        json.dumps({'passed': True, 'purge_posts': len(purge_posts), 'cleanup_requests': len(cleanup_requests), 'errors': errors}), encoding='utf-8')
    browser.close()
print('P33 browser passed' + (' (disabled, no cleanup calls).' if disabled else ' (confirmation, failed/cancelled backup, double-submit, lost response, recovery, staff and mobile).'))
