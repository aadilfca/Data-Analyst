"""Owned P29 native UI and intercepted legacy-protocol checks; no live data."""
import json
import sys
from pathlib import Path
from urllib.parse import urlparse
from playwright.sync_api import sync_playwright, expect

base = sys.argv[1].rstrip('/')
evidence = Path(sys.argv[2]).resolve()
assert urlparse(base).hostname == '127.0.0.1'
evidence.mkdir(parents=True, exist_ok=True)


def check(condition, message):
    if not condition:
        raise AssertionError(message)


with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    context = browser.new_context(viewport={"width": 1440, "height": 1080}, accept_downloads=True)
    context.add_cookies([{"name": "session_token", "value": "a" * 64, "url": base}])
    page = context.new_page()
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    requests = []
    page.on('request', lambda request: requests.append((request.method, request.url)))

    def api(path, body=None, revision=None, method=None):
        return page.evaluate("""async args => {
            const headers = {'Content-Type':'application/json'};
            if (args.revision) headers['X-Template-Revision'] = args.revision;
            const response = await fetch(args.path, {method:args.method || (args.body ? 'POST':'GET'), headers,
                body:args.body ? JSON.stringify(args.body):undefined});
            return {status:response.status, data:await response.json()};
        }""", {"path": path, "body": body, "revision": revision, "method": method})

    def choose_backup(value):
        # Use the real picker action: it waits for readiness and clears the
        # previous selection so choosing the same file raises a fresh change.
        trigger = page.get_by_role('button', name='Choose backup to review', exact=True)
        expect(trigger).to_be_enabled()
        with page.expect_file_chooser() as chooser:
            trigger.click()
        chooser.value.set_files(value)

    page.goto(base + '/static/setup.html?feature=configuration-backup')
    expect(page.locator('#configurationBackup')).to_be_visible()
    expect(page.locator('#legacySetupTransfer')).to_be_hidden()
    if len(sys.argv) > 3 and sys.argv[3] == '--disabled':
        expect(page.locator('#exportConfigurationBtn')).to_be_disabled()
        expect(page.locator('#selectConfigurationBtn')).to_be_disabled()
        check(not any('/api/backup/' in url or '/api/database-backup/' in url for _, url in requests), 'Disabled native UI made a backup request')
        check(not errors, errors)
        page.screenshot(path=str(evidence / 'configuration-disabled.png'), full_page=True)
        browser.close()
        sys.exit(0)
    expect(page.locator('#exportConfigurationBtn')).to_be_enabled()
    check(page.evaluate("NAV_MANIFEST.some(g => (g.items || []).some(i => (i.children || []).some(c => c.url === '/static/setup.html?feature=configuration-backup')))"), 'Native backup is absent from the navigation registry')
    check(not any('/api/database-backup/' in url for _, url in requests), 'Native page probed a legacy full-backup route')
    templates = api('/api/templates')['data']
    template = next(t for t in templates if t['template_name'] == 'Purchase Order')
    template_id = template['id']
    columns = api('/api/columns')['data']
    ids = {c['column_name']: c['id'] for c in columns}
    original_client = api('/api/auth/current-client')['data']

    def open_editor():
        page.goto(base + '/static/templates.html?feature=accounting-fields')
        expect(page.locator('#templatesBody button[title="Edit template"]').first).to_be_visible()
        page.locator('#templatesBody button[title="Edit template"]').first.click()
        expect(page.locator('#accounting-field-amount')).to_be_visible()

    def save_editor():
        page.locator('#saveTemplateBtn').click()
        expect(page.locator('#templateModal')).to_be_hidden(timeout=15000)

    open_editor()
    expect(page.locator('#accounting-field-amount')).to_have_value(str(ids['Amount']))
    expect(page.locator('#accounting-field-rate')).to_have_value(str(ids['Rate']))
    check(page.locator('#accounting-field-amount option[value="%s"]' % ids['Document']).count() == 0, 'Text offered as Amount')
    page.locator('#accounting-field-rate').select_option(str(ids['Amount']))
    page.locator('#saveTemplateBtn').click()
    expect(page.locator('#modalStatus')).to_contain_text('Rate must use a different column')
    page.locator('#accounting-field-rate').select_option(str(ids['Rate']))
    page.locator('#accounting-field-amount').select_option(str(ids['Alternate Amount']))
    page.locator('#templateName').fill('Renamed <Accounting>')
    page.screenshot(path=str(evidence / 'accounting-fields.png'), full_page=True)
    save_editor()
    open_editor()
    expect(page.locator('#accounting-field-amount')).to_have_value(str(ids['Alternate Amount']))
    expect(page.locator('#rateSourceField')).to_be_visible()
    page.locator('#cancelTemplateBtn').click()
    # Column labels and ordering change without rebinding their permanent IDs.
    template = api('/api/templates/%s' % template_id)['data']
    reordered = list(reversed([c['id'] for c in template['columns']]))
    result = api('/api/templates/%s/reorder' % template_id, reordered, template['configuration_key'])
    check(result['status'] == 200, result)
    open_editor()
    expect(page.locator('#accounting-field-amount')).to_have_value(str(ids['Alternate Amount']))
    # A bound column removal remains an explicit review, rather than clearing it.
    page.locator('#selectedColumnsList .selected-item[data-id="%s"] button' % ids['Alternate Amount']).click()
    page.locator('#saveTemplateBtn').click()
    expect(page.locator('#modalStatus')).to_contain_text('Review the accounting assignments')
    page.locator('#cancelTemplateBtn').click()

    page.goto(base + '/static/setup.html?feature=configuration-backup')
    with page.expect_download() as download:
        page.locator('#exportConfigurationBtn').click()
    downloaded = evidence / 'configuration.json'
    download.value.save_as(str(downloaded))
    source = json.loads(downloaded.read_text(encoding='utf-8'))
    check(source['version'] == 2, 'Export did not record version 2')
    check(source['templates'][0]['uploadBindings']['amount'] == ids['Alternate Amount'], 'Export lost assignments')
    # The exact source text must survive preview and restore: JS numbers cannot
    # represent this valid Int64 revision counter without changing its value.
    source['templates'][0]['version'] = 9007199254740993
    payload = json.dumps(source, ensure_ascii=False).encode('utf-8')
    file = {"name": "configuration.json", "mimeType": "application/json", "buffer": payload}
    choose_backup(file)
    expect(page.locator('#configurationRestorePreview')).to_be_visible()
    expect(page.locator('#configurationRestoreItems')).to_contain_text('Amount: Alternate Amount')
    check(page.locator('#configurationRestoreItems Accounting').count() == 0, 'A template label became HTML')
    expect(page.locator('#restoreConfigurationBtn')).to_be_disabled()
    page.screenshot(path=str(evidence / 'configuration-preview.png'), full_page=True)
    # An intervening edit invalidates the server's preview token.
    template = api('/api/templates/%s' % template_id)['data']
    changed = {"template_name": "Changed after preview", "selected_columns": [c['id'] for c in template['columns']]}
    check(api('/api/templates/%s' % template_id, changed, template['configuration_key'], 'PUT')['status'] == 200, 'Concurrent edit failed')
    page.locator('#confirmConfigurationRestore').check()
    page.locator('#restoreConfigurationBtn').click()
    expect(page.locator('#configurationBackupStatus')).to_contain_text('Configuration changed')
    expect(page.locator('#configurationRestorePreview')).to_be_hidden()
    choose_backup(file)
    expect(page.locator('#configurationRestorePreview')).to_be_visible()
    page.locator('#confirmConfigurationRestore').check()
    import_bodies = []
    page.on('request', lambda request: import_bodies.append(request.post_data) if request.url.endswith('/api/backup/import') else None)
    page.locator('#restoreConfigurationBtn').click()
    expect(page.locator('#configurationBackupStatus')).to_contain_text('Configuration restored')
    check('9007199254740993' in import_bodies[-1], 'The backup version counter was rounded by the browser')
    # A committed restore with a lost HTTP response never submits automatically again.
    choose_backup(file)
    expect(page.locator('#configurationRestorePreview')).to_be_visible()
    def lose_response(route):
        response = route.fetch()
        check(response.status == 200, 'Synthetic lost response did not commit')
        route.abort()
    page.route('**/api/backup/import', lose_response)
    page.locator('#confirmConfigurationRestore').check()
    before = len(import_bodies)
    page.locator('#restoreConfigurationBtn').click()
    expect(page.locator('#configurationBackupStatus')).to_contain_text('fresh preview')
    expect(page.locator('#restoreConfigurationBtn')).to_be_disabled()
    check(len(import_bodies) == before + 1, 'Uncertain restore was submitted twice')
    page.unroute('**/api/backup/import', lose_response)
    # Oversized files are stopped before upload/parsing, and stale UI is cleared.
    before = sum(url.endswith('/api/backup/preview') for _, url in requests)
    choose_backup({"name":"large.json","mimeType":"application/json","buffer":b' ' * (7 * 1024 * 1024 + 1)})
    expect(page.locator('#configurationBackupStatus')).to_contain_text('7 MiB')
    check(sum(url.endswith('/api/backup/preview') for _, url in requests) == before, 'Oversized file reached the API')
    open_editor()
    page.locator('#accounting-field-amount').select_option(str(ids['Amount']))
    save_editor()
    check(not errors, errors)

    member = browser.new_context()
    member.add_cookies([{"name":"session_token","value":"c" * 64,"url":base}])
    member_page = member.new_page()
    member_page.goto(base + '/static/templates.html')
    expect(member_page.locator('#addTemplateBtn')).to_be_disabled()
    expect(member_page.locator('#templatesReadOnlyNotice')).to_be_visible()
    check(member_page.goto(base + '/static/setup.html').status == 403, 'Member opened administrator Setup')
    member.close()

    # The same shared pages retain the Python wire protocol. Interception keeps
    # this comparison hermetic; no Python database or router is started.
    legacy = browser.new_context(accept_downloads=True)
    legacy.add_cookies([{"name":"session_token","value":"a" * 64,"url":base}])
    legacy_page = legacy.new_page()
    legacy.route('**/shell/context.js', lambda route: route.fulfill(status=200, content_type='text/javascript', body=''))
    imported = []
    saved = []
    legacy_backup = {"columns":[],"templates":[],"client_data":[]}
    def legacy_api(route):
        path = urlparse(route.request.url).path
        method = route.request.method
        if path == '/api/auth/current-client': data = original_client
        elif path == '/api/users': data = [{"id":1,"username":"admin-a","role":"admin","is_active":True}]
        elif path == '/api/columns': data = columns
        elif path == '/api/templates' and method == 'GET': data = [template]
        elif path.startswith('/api/templates') and method in ('POST','PUT'):
            saved.append(route.request.post_data_json); data = {"message":"Saved","template_id":template_id}
        elif path == '/api/backup/export': data = legacy_backup
        elif path == '/api/backup/import': imported.append(route.request.post_data_json); data = {"message":"Legacy transfer checked"}
        elif path.startswith('/api/database-backup/'):
            route.fulfill(status=404, content_type='application/json', body='{}'); return
        elif path == '/api/settings': data = {"date_format":"DD/MM/YYYY"}
        else: data = {}
        route.fulfill(status=200, content_type='application/json', body=json.dumps(data))
    legacy.route('**/api/**', legacy_api)
    legacy_page.on('dialog', lambda dialog: dialog.accept())
    legacy_page.goto(base + '/static/setup.html')
    expect(legacy_page.locator('#configurationBackup')).to_be_hidden()
    expect(legacy_page.locator('#legacySetupTransfer')).to_be_visible()
    legacy_page.locator('#importFileInput').set_input_files({"name":"legacy.json","mimeType":"application/json","buffer":json.dumps(legacy_backup).encode()})
    expect(legacy_page.locator('#backupStatus')).to_contain_text('Legacy transfer checked')
    check(imported == [legacy_backup], 'Legacy import acquired a native wrapper')
    legacy_page.goto(base + '/static/templates.html')
    legacy_page.locator('#templatesBody button[title="Edit template"]').first.click()
    expect(legacy_page.locator('#accountingBindingFields')).to_be_hidden()
    legacy_page.locator('#saveTemplateBtn').click()
    expect(legacy_page.locator('#templateModal')).to_be_hidden()
    check(saved and 'upload_bindings' not in saved[-1], 'Native bindings leaked into the Python payload')
    legacy.close()
    browser.close()
    print('P29 native editor, exact backup text, stale/uncertain restore, member access and legacy protocol checks passed.')
