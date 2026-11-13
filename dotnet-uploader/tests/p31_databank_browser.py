"""Real native history/download UI; generated workbooks contain synthetic data only."""
import json
import sys
from pathlib import Path
from urllib.parse import urlparse

import openpyxl
from playwright.sync_api import sync_playwright, expect

directory = Path(sys.argv[1]).resolve()
fixture = json.loads((directory / 'fixture.json').read_text())
address = fixture['address']
assert urlparse(address).hostname == '127.0.0.1' and directory.name.startswith('p31-')
disabled = len(sys.argv) > 2 and sys.argv[2] == 'disabled'
with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    context = browser.new_context(viewport={'width':1366,'height':900}, accept_downloads=True)
    context.add_cookies([{'name':'session_token','value':'a'*64,'url':address}])
    page = context.new_page(); errors, calls, downloads = [], [], []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.on('request', lambda request: calls.append(request.url))
    page.on('download', lambda download: downloads.append(download.suggested_filename))
    page.goto(address + '/static/databank.html')
    expect(page.locator('#yourDatasetsBody tr[data-template-id]')).to_have_count(4)
    if disabled:
        assert page.get_by_role('button', name='Changed/Deleted data for Transactions', exact=True).count() == 0
        expect(page.locator('#nativeExportMatching')).to_be_hidden()
        assert not any('/api/data/history?' in url or '/backup?' in url for url in calls)
        result = page.evaluate("async () => (await fetch('/api/data/history?client_id=10&template_id=1')).status")
        assert result == 503
        (directory / 'disabled-results.json').write_text(json.dumps({'accepted':True}))
        assert not errors, errors
        browser.close();sys.exit(0)

    page.get_by_role('button', name='Changed/Deleted data for Transactions', exact=True).click()
    expect(page.locator('#historyStatus')).to_contain_text('7 records')
    expect(page.locator('#historyLedgerControl')).to_be_hidden()
    page.locator('#historyActionFilter').select_option('changed')
    expect(page.locator('#historyStatus')).to_contain_text('3 records')
    expect(page.locator('#historyTableBody')).to_contain_text('9007199254740993.01')
    expect(page.locator('#historyTableBody')).to_contain_text('9007199254740993.10')
    expect(page.locator('#historyTableBody')).to_contain_text('Field-by-field details were not saved')
    page.screenshot(path=str(directory / 'history.png'), full_page=True)
    page.locator('#historyActionFilter').select_option('deleted')
    expect(page.locator('#historyStatus')).to_contain_text('4 records')
    expect(page.locator('#historyTableBody')).not_to_contain_text('Unique')
    page.locator('#closeHistoryBtn').click()
    page.get_by_role('button', name='Changed/Deleted data for Ledger', exact=True).click()
    expect(page.locator('#historyLedgerSelect option[value="000OLD"]')).to_have_count(1)
    page.locator('#historyLedgerSelect').select_option('000OLD')
    expect(page.locator('#historyStatus')).to_contain_text('1 record')
    expect(page.locator('#historyTableBody')).to_contain_text('001 DR')
    page.locator('#closeHistoryBtn').click()
    page.evaluate("""async () => {sharedRangeFromInput.value='2026-04-01';sharedRangeToInput.value='2026-04-30';await openPreview(1,'Transactions');}""")
    expect(page.locator('#previewCountInfo')).to_contain_text('5')
    page.set_viewport_size({'width':390,'height':844})
    layout=page.evaluate("""() => ({width:innerWidth,scroll:document.documentElement.scrollWidth,
        wide:Array.from(document.querySelectorAll('body *')).map(el=>({tag:el.tagName,id:el.id,cls:el.className,left:el.getBoundingClientRect().left,right:el.getBoundingClientRect().right,width:el.getBoundingClientRect().width})).filter(el=>el.right>innerWidth+1&&el.width>0).slice(0,18)})""")
    (directory/'mobile-layout.json').write_text(json.dumps(layout,indent=2))
    page.screenshot(path=str(directory/'mobile.png'),full_page=True)
    control=page.locator('#nativeExportMatching').bounding_box()
    modal=page.locator('#previewModal .modal-content').bounding_box()
    assert control['x']>=modal['x']-1 and control['x']+control['width']<=modal['x']+modal['width']+1
    baseline=page.evaluate("""() => {const button=document.getElementById('nativeExportMatching');button.hidden=true;const width=document.documentElement.scrollWidth;button.hidden=false;return width;}""")
    assert layout['scroll']<=baseline+1,layout
    layout['baseline_without_export_control']=baseline
    (directory/'mobile-layout.json').write_text(json.dumps(layout,indent=2))
    page.set_viewport_size({'width':1366,'height':900})
    page.locator('#previewTableBody .history-icon').first.click()
    expect(page.locator('#rowHistoryBody')).to_contain_text('Saved upload changes')
    expect(page.locator('#rowHistoryBody')).to_contain_text('9007199254740993.01')
    page.get_by_role('combobox', name='Revision source').select_option('legacy')
    expect(page.locator('#rowHistoryBody')).to_contain_text('Legacy')
    expect(page.locator('#rowHistoryBody')).to_contain_text('INSERT')
    page.evaluate('hideRowHistoryPopover()')
    page.locator('#searchInput').fill('literal')
    expect(page.locator('#nativeExportMatching')).to_be_disabled()
    page.locator('#applyFiltersBtn').click()
    expect(page.locator('#previewCountInfo')).to_contain_text('1')
    expect(page.locator('#nativeExportMatching')).to_be_enabled()
    with page.expect_download() as pending:
        page.locator('#nativeExportMatching').click()
    download=pending.value;download.save_as(directory / 'matching-browser.xlsx')
    book=openpyxl.load_workbook(directory / 'matching-browser.xlsx')
    assert book.sheetnames == ['Data'] and book['Data'].max_row == 6
    assert any(cell.value == '9007199254740993.10' for row in book['Data'] for cell in row)
    download_count=len(downloads)
    for cancel in (False, True):
        page.evaluate("""async cancel => {
            const real=window.fetch;let release,ready;const held=new Promise(resolve=>ready=resolve);
            window.fetch=async(...args)=>{const response=await real(...args);if(String(args[0]).includes('/export?')){ready();await new Promise(resolve=>release=resolve);}return response;};
            try{const old=nativeDownload(1);await held;
                if(cancel)cancelNativeDownload();else searchInput.value='different selection';
                release();await old;
            }finally{window.fetch=real;searchInput.value='literal';updateNativeExportButton();}
        }""",cancel)
        expect(page.locator('#nativeDownloadStatus')).to_contain_text('cancelled' if cancel else 'Selection changed')
        assert len(downloads)==download_count
    page.locator('#closePreviewBtn').click()
    with page.expect_download() as pending:
        page.get_by_role('button', name='Back up all current data and history for History only', exact=True).click()
    pending.value.save_as(directory / 'history-only-browser.xlsx')
    book=openpyxl.load_workbook(directory / 'history-only-browser.xlsx')
    assert book['Data'].max_row == 5 and book['Changed-Deleted History'].max_row == 6
    expect(page.locator('#nativeDownloadStatus')).to_contain_text('No data was deleted')
    # A held real history response cannot repopulate a closed/different selection.
    page.evaluate("""async () => {
        const real=window.fetch;let release,ready;const held=new Promise(resolve=>ready=resolve);
        window.fetch=async(...args)=>{const response=await real(...args);if(String(args[0]).includes('/api/data/history?')){ready();await new Promise(resolve=>release=resolve);}return response;};
        try{const old=openUploadHistory({template_id:1,template_name:'Transactions'});await held;window.onClientChange(null);release();await old;
            if(historyModal.style.display==='flex')throw new Error('Old history reopened');
        }finally{window.fetch=real;}
    }""")
    # Administrator-only backup capability is also enforced by the server.
    staff=browser.new_context();staff.add_cookies([{'name':'session_token','value':'c'*64,'url':address}]);staff_page=staff.new_page();staff_page.goto(address+'/static/databank.html')
    expect(staff_page.locator('#yourDatasetsBody tr[data-template-id]')).to_have_count(4)
    assert staff_page.get_by_role('button', name='Back up all current data and history for Transactions', exact=True).count()==0
    assert staff_page.evaluate("async()=> (await fetch('/api/data/template/1/backup?client_id=10')).status")==403
    staff.close()
    assert not errors, errors
    assert not any('/data-purge' in url for url in calls)
    (directory/'browser-results.json').write_text(json.dumps({'accepted':True,'page_errors':errors,'requests':len(calls)},indent=2))
    browser.close()
