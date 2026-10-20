import pytest
from pathlib import Path

from playwright.sync_api import sync_playwright


TEMPLATES_HTML = Path(__file__).resolve().parents[1] / "static" / "templates.html"
TEMPLATES_JS = Path(__file__).resolve().parents[1] / "static" / "templates.js"


def test_edit_template_keeps_column_draft_until_explicit_save():
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        fetch_calls = []

        page.on("console", lambda message: None)
        page.set_content(TEMPLATES_HTML.read_text(encoding="utf-8"))
        page.add_script_tag(content="""
            var API_BASE = '';
            window.formatDate = value => value || '';
            window.LoadingUI = {
                showSpinner() {},
                hideSpinner() {}
            };
            window.fetch = async (url, options = {}) => {
                window.__fetchCalls.push({url, method: options.method || 'GET'});
                if (url === '/api/columns') {
                    return new Response(JSON.stringify([
                        {id: 1, column_name: 'Quantity', data_type: 'Numeric', notes: null}
                    ]), {status: 200, headers: {'Content-Type': 'application/json'}});
                }
                if (String(url).startsWith('/api/templates/')) {
                    return new Response(JSON.stringify({message: 'Template updated'}), {
                        status: 200, headers: {'Content-Type': 'application/json'}
                    });
                }
                return new Response(JSON.stringify([]), {
                    status: 200, headers: {'Content-Type': 'application/json'}
                });
            };
            window.__fetchCalls = [];
        """)
        page.add_script_tag(path=str(TEMPLATES_JS))
        try:
            page.evaluate("""async () => {
                await openModal('Edit Template', {
                    id: 7,
                    template_name: 'Disposable test template',
                    columns: []
                });
                searchInput.value = 'Quantity';
                renderSearchResults('Quantity');
                document.querySelector('#searchResults button').click();
            }""")

            assert page.locator("#selectedCountBadge").text_content() == "1 selected"
            assert page.locator("#templateModal").evaluate("node => node.style.display") == "block"
            assert page.locator("#templateModalOverlay").evaluate("node => node.style.display") == "block"
            assert page.evaluate("window.__fetchCalls.filter(call => call.method === 'PUT').length") == 0

            page.locator("#templateModalOverlay").click(position={"x": 5, "y": 5})
            assert page.locator("#templateModal").evaluate("node => node.style.display") == "block"
            page.keyboard.press("Escape")
            assert page.locator("#templateModal").evaluate("node => node.style.display") == "block"

            page.locator("#closeModalBtn").click()
            assert page.locator("#templateModal").evaluate("node => node.style.display") == "none"

            page.evaluate("""async () => {
                await openModal('Edit Template', {
                    id: 7,
                    template_name: 'Disposable test template',
                    columns: [{id: 1, column_name: 'Quantity', data_type: 'Numeric'}]
                });
            }""")
            assert page.locator("#selectedCountBadge").text_content() == "1 selected"
            assert page.locator("#selectedColumnsList .selected-item > span:nth-of-type(3) > span").text_content() == "Quantity"

            page.locator("#saveTemplateBtn").click()
            page.wait_for_timeout(900)
            assert page.evaluate("window.__fetchCalls.filter(call => call.method === 'PUT').length") == 1
        finally:
            browser.close()


@pytest.fixture
def ledger_template_page():
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content(TEMPLATES_HTML.read_text(encoding='utf-8'))
        page.add_script_tag(content="""
            var API_BASE = ''; window.formatDate = value => value || '';
            window.LoadingUI = {showSpinner(){}, hideSpinner(){}};
            window.__calls = []; window.__templates = [];
            window.__columns = ['Voucher ID','Transaction Date','Voucher Detail','Debit Amount','Credit Amount'].map((name,i)=>({
                id:i+1,column_name:name,data_type:['Text','Date','Text','Numeric','Numeric'][i],category:'Financial',notes:null}));
            window.fetch = async (url, options={}) => {
                window.__calls.push({url, method:options.method || 'GET', body: options.body ? JSON.parse(options.body) : null});
                let result = [];
                if(url === '/api/columns') result = window.__columns;
                else if(url === '/api/templates') result = options.method ? {message:'Saved',template_id:10} : window.__templates;
                else if(url === '/api/templates/ledger-setup') {
                    if(options.method && !window.__columns.some(c=>c.id===6)) window.__columns.push({id:6,column_name:'Balance',data_type:'Text',category:'Financial',notes:null});
                    result = options.method ? {message:'Ledger saved',template_id:10} : {
                        template_name:'Account Ledger',columns:['voucher','date','description','debit','credit','balance'].map((role,i)=>({
                            role,column_name:['Voucher ID','Transaction Date','Voucher Detail','Debit Amount','Credit Amount','Balance'][i],
                            data_type:['Text','Date','Text','Numeric','Numeric','Text'][i],column_id:i<5?i+1:null}))};
                } else if(url.startsWith('/api/templates/')) result = {message:'Saved'};
                return new Response(JSON.stringify(result),{status:200,headers:{'Content-Type':'application/json'}});
            };
        """)
        page.add_script_tag(content=TEMPLATES_JS.read_text(encoding='utf-8'))
        page.evaluate('async () => { await loadTemplates(); }')
        yield page
        browser.close()


def test_prepare_ledger_is_a_draft_until_save_and_reuses_existing_columns(ledger_template_page):
    page = ledger_template_page
    page.locator('#prepareLedgerBtn').click()
    assert page.locator('#templatePurpose').input_value() == 'account_ledger'
    assert page.locator('#templatePurpose').is_disabled()
    assert page.locator('#selectedCountBadge').inner_text() == '6 selected'
    assert page.locator('#ledgerRole-balance').input_value() == '-6'
    assert 'create on Save' in page.locator('#selectedColumnsList').inner_text()
    assert page.evaluate("window.__calls.filter(c => c.method === 'POST').length") == 0
    page.locator('#saveTemplateBtn').click()
    page.wait_for_function("window.__calls.some(c => c.method === 'POST')")
    request = page.evaluate("window.__calls.find(c => c.method === 'POST')")
    assert request['url'] == '/api/templates/ledger-setup'
    assert request['body'] == {'template_name':'Account Ledger', 'has_subtotals_by_default':False, 'ledger_columns':{
        'voucher':1,'date':2,'description':3,'debit':4,'credit':5,'balance':None}}
    assert page.evaluate("window.__calls.filter(c => c.url === '/api/columns' && c.method === 'POST').length") == 0
    page.wait_for_function("document.getElementById('templateModal').style.display === 'none'")
    page.locator('#addTemplateBtn').click()
    page.locator('#columnSearchInput').fill('Balance')
    assert page.locator('#searchResults').inner_text().startswith('Balance')


def test_cancelled_ledger_draft_has_no_writes_and_standard_editor_stays_standard(ledger_template_page):
    page = ledger_template_page
    page.locator('#prepareLedgerBtn').click()
    page.locator('#cancelTemplateBtn').click()
    page.locator('#addTemplateBtn').click()
    assert page.locator('#templatePurpose').input_value() == 'standard'
    assert page.locator('#templatePurpose').is_enabled()
    assert page.locator('#ledgerRoleFields').is_hidden()
    assert page.locator('#templateColumnSearch').is_visible()
    assert page.evaluate("window.__calls.filter(c => c.method === 'POST').length") == 0


def test_existing_ledger_roles_are_preserved_and_wrong_voucher_order_blocks_save(ledger_template_page):
    page = ledger_template_page
    page.evaluate("""async () => {
        const columns = [...window.__columns,{id:6,column_name:'Balance',data_type:'Text'}];
        await openModal('Edit ledger',{id:10,template_name:'Account Ledger',data_purpose:'account_ledger',
            ledger_columns:{voucher:1,date:2,description:3,debit:4,credit:5,balance:6},columns});
    }""")
    assert page.locator('#ledgerRole-balance').input_value() == '6'
    assert page.locator('#ledgerRole-voucher option[value="4"]').count() == 0
    page.locator('#saveTemplateBtn').click()
    page.wait_for_function("window.__calls.some(c => c.method === 'PUT')")
    request = page.evaluate("window.__calls.find(c => c.method === 'PUT')")
    assert request['body']['data_purpose'] == 'account_ledger'
    assert request['body']['ledger_columns']['balance'] == 6
    page.wait_for_function("document.getElementById('templateModal').style.display === 'none'")
    page.evaluate("""async () => {
        await openModal('Wrong order',{id:10,template_name:'Ledger',data_purpose:'account_ledger',
            ledger_columns:{voucher:1,date:2,description:3,debit:4,credit:5,balance:6},
            columns:[window.__columns[1],window.__columns[0],...window.__columns.slice(2),{id:6,column_name:'Balance',data_type:'Text'}]});
    }""")
    page.locator('#saveTemplateBtn').click()
    assert 'keep the Voucher column first' in page.locator('#modalStatus').inner_text()
    assert page.evaluate("window.__calls.filter(c => c.method === 'PUT').length") == 1
