"""Real native Kestrel pages; SQL data comes from DataBankTests' owned fixture."""
import json
import sys
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright, expect

directory = Path(sys.argv[1]).resolve()
fixture = json.loads((directory / 'fixture.json').read_text())
address = fixture['address']
assert urlparse(address).hostname == '127.0.0.1' and directory.name.startswith('p30-')
disabled = len(sys.argv) > 2 and sys.argv[2] == 'disabled'
with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    context = browser.new_context(viewport={'width': 1366, 'height': 900})
    context.add_cookies([{'name': 'session_token', 'value': 'a' * 64, 'url': address}])
    page = context.new_page()
    errors, requests = [], []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.on('request', lambda req: requests.append(req.url))
    page.goto(address + '/static/databank.html')
    if disabled:
        expect(page.locator('#statusMessage')).to_contain_text('disabled')
        assert not any('/api/data/' in url for url in requests)
        page.goto(address + '/static/template-viewer.html?template=1')
        expect(page.locator('#tvStatus')).to_contain_text('disabled')
        assert not any('/api/data/' in url for url in requests)
        assert not errors, errors
        (directory / 'disabled-results.json').write_text(json.dumps({'accepted': True}))
        browser.close()
        sys.exit(0)

    expect(page.locator('#yourDatasetsBody tr[data-template-id]')).to_have_count(5)
    expect(page.locator('[data-availability-metric="documents"]').first).not_to_have_text('Calculating…')
    assert page.locator('.action-btn.export, .action-btn.delete').count() == 0
    assert page.get_by_role('button', name='Changed/Deleted data for Transactions', exact=True).count() == 0
    assert page.locator('#yourDatasetsBody select').count() == 0
    # Drive the retained toolbar and preview renderer, backed by real native reads.
    page.evaluate("""async () => {
        sharedRangeFromInput.value='2026-04-01'; sharedRangeToInput.value='2026-04-30';
        await fetchAvailability(); await openPreview(1,'Transactions');
    }""")
    expect(page.locator('#previewCountInfo')).to_contain_text('4')
    expect(page.locator('#previewMetrics')).to_contain_text('9,007,199,254,740,992.00')
    expect(page.locator('#previewTableBody')).to_contain_text('9007199254740993.01')
    expect(page.locator('#previewTableBody')).to_contain_text('9007199254740993.10')  # Exact page subtotal.
    assert page.locator('#previewTableBody safe').count() == 0
    page.screenshot(path=str(directory / 'preview.png'), full_page=True)
    # Search changes server totals; subsequent page/subtotal choices never broaden them.
    page.locator('#searchInput').fill("O'Reilly")
    page.locator('#applyFiltersBtn').click()
    expect(page.locator('#previewMetrics')).to_contain_text('9,007,199,254,740,993.10')
    page.locator('#subtotalToggleCheckbox').uncheck()
    expect(page.locator('#previewMetrics')).to_contain_text('9,007,199,254,740,993.10')
    # Hold the older real response until the newer selection has rendered.
    page.evaluate("""async () => {
        const real=window.ApiUtils.apiFetch; let release, ready;
        const held=new Promise(resolve=>ready=resolve);
        window.ApiUtils.apiFetch=async (...args)=>{
            const response=await real(...args);
            if(String(args[0]).includes('search=Sunday')) { ready(); await new Promise(resolve=>release=resolve); }
            return response;
        };
        try {
            searchInput.value='Sunday'; const old=fetchPreviewData(); await held;
            searchInput.value="O'Reilly"; await fetchPreviewData(); release(); await old;
        } finally { window.ApiUtils.apiFetch=real; }
    }""")
    expect(page.locator('#previewMetrics')).to_contain_text('9,007,199,254,740,993.10')
    # Refuse wrong-scope totals and clear prior numbers on failure.
    def wrong_scope(route):
        response=route.fetch(); data=response.json(); data['preview_metrics']['scope']['client_id']=20
        route.fulfill(response=response,json=data)
    page.route('**/api/data/preview?**', wrong_scope)
    page.evaluate("invalidateDataBankCaches(); previewPageInFlight.clear(); void fetchPreviewData()")
    expect(page.locator('#previewMetrics')).to_contain_text('Totals unavailable')
    assert '9,007' not in page.locator('#previewMetrics').inner_text()
    page.unroute_all(behavior='wait')
    page.evaluate("invalidateDataBankCaches(); previewPageInFlight.clear(); void fetchPreviewData()")
    expect(page.locator('#previewMetrics')).to_contain_text('9,007,199,254,740,993.10')
    # Account choice remains mandatory and uses exact uploaded codes.
    page.evaluate("void openPreview(2,'Ledger')")
    expect(page.locator('#previewLedgerInput')).to_be_visible()
    expect(page.locator('#previewLedgerStatus')).to_contain_text('account')
    page.locator('#previewLedgerInput').fill('Latest')
    page.locator('#previewLedgerOptions [role="option"]').filter(has_text='Latest name').click()
    expect(page.locator('#previewCountInfo')).to_contain_text('2')
    expect(page.locator('#previewTableBody')).to_contain_text('100 DR')
    expect(page.locator('#previewTableBody')).to_contain_text('102 DR')
    page.evaluate("closePreviewBtn.click(); void openTemplateCalendarPopover(1,'Transactions')")
    expect(page.locator('#coverageCalendarsContainer')).to_contain_text('Transactions')
    page.evaluate("calendarYear=2026; calendarMonth=4; void fetchTemplateCalendar()")
    expect(page.locator('#calendarMonthLabel')).to_contain_text('2026')
    expect(page.locator('#coverageCalendarsContainer')).to_contain_text('Holiday')
    # Slow/unavailable secondary metrics must leave the dataset list available.
    page.route('**/api/data/availability/metrics?**', lambda route: route.fulfill(status=503,json={'detail':'synthetic metrics failure'}))
    page.evaluate("closeTemplateCalendarPopover(); invalidateDataBankCaches(); void fetchAvailability()")
    expect(page.locator('#clientDbSize')).to_have_text('Unavailable')
    expect(page.locator('#yourDatasetsBody tr[data-template-id]')).to_have_count(5)
    page.unroute_all(behavior='wait')
    # A metadata read completing after invalidation cannot refill the old cache.
    page.evaluate("""async () => {
        invalidateDataBankCaches(); const real=window.fetch; let release, ready;
        const held=new Promise(resolve=>ready=resolve);
        window.fetch=async (...args)=>{
            const response=await real(...args);
            if(String(args[0]).endsWith('/api/templates/1')) { ready(); await new Promise(resolve=>release=resolve); }
            return response;
        };
        try {
            const old=fetchTemplateSubtotalDefault(1); await held;
            invalidateDataBankCaches(); release(); await old;
            if(Object.prototype.hasOwnProperty.call(templatePurposeCache,1)) throw new Error('Obsolete metadata was cached');
        } finally {window.fetch=real;}
    }""")
    page.evaluate("void openPreview(1,'Transactions')")
    expect(page.locator('#previewCountInfo')).to_contain_text('4')
    page.evaluate("window.onClientChange(null)")
    expect(page.locator('#previewModal')).to_be_hidden()
    expect(page.locator('#yourDatasetsBody')).not_to_contain_text('Transactions')
    # Bounded 200-row viewer retains exact numeric text and marks truncation.
    page.goto(address + '/static/template-viewer.html?template=1')
    expect(page.locator('#templateViewerTitle')).to_contain_text('Transactions')
    page.locator('#tvStartDate').fill('2026-04-01'); page.locator('#tvEndDate').fill('2026-04-30');page.locator('#tvFetchBtn').click()
    expect(page.locator('#tvDataTable')).to_contain_text('9007199254740993.01')
    page.locator('#tvSearchInput').fill('9007199254740993.01')
    expect(page.locator('#tvDataTable tbody tr')).to_have_count(1)
    page.goto(address + '/static/template-viewer.html?template=5')
    expect(page.locator('#templateViewerTitle')).to_contain_text('Large')
    page.locator('#tvStartDate').fill('2026-04-01');page.locator('#tvEndDate').fill('2026-04-30');page.locator('#tvFetchBtn').click()
    expect(page.locator('#tvStatus')).to_contain_text('200 of 10000')
    assert page.locator('#tvDataTable tbody tr').count() == 200
    assert not errors, errors
    (directory / 'browser-results.json').write_text(json.dumps({'accepted':True,'requests':len(requests),'page_errors':errors},indent=2))
    browser.close()
