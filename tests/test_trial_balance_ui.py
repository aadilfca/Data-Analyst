"""Hermetic browser coverage for Trial Balance mapping and validation views."""

from pathlib import Path

import pytest

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import sync_playwright  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as playwright:
        try:
            instance = playwright.chromium.launch(headless=True)
        except Exception as exc:
            pytest.skip("Chromium not available: {}".format(exc))
        try:
            yield instance
        finally:
            instance.close()


def test_validation_grid_shows_mapped_columns_only_and_preserves_source_preview(browser):
    page = browser.new_page()
    page.set_content("""
        <table><thead id="mappingPreviewHead"></thead><tbody id="mappingPreviewBody"></tbody></table>
        <table><thead id="tbValidationHead"></thead><tbody id="tbValidationBody"></tbody></table>
    """)
    page.add_script_tag(path=str(PROJECT_ROOT / "static" / "trial_balance.js"))

    result = page.evaluate("""() => {
        fileHeaders = [
            'Account Code', 'Account Name', 'Opening Debit', 'Opening Credit',
            'Period Debit', 'Period Credit', 'Closing Debit', 'Closing Credit',
            'Start Date', 'End Date', 'Unnamed: 10'
        ];
        tbColumnMapping = {
            account_code_column: 'Account Code',
            account_name_column: 'Account Name',
            opening_dr_column: 'Opening Debit',
            opening_cr_column: 'Opening Credit',
            period_dr_column: 'Period Debit',
            period_cr_column: 'Period Credit',
            closing_dr_column: 'Closing Debit',
            closing_cr_column: 'Closing Credit'
        };
        const sourceRow = [
            '1', 'ASSETS', '100', '0', '50', '0', '150', '0',
            '2025-07-01', '2026-06-30', '3222222.22'
        ];
        filePreviewRows = [sourceRow];
        fileAllRows = [sourceRow];
        renderMappingPreviewTable();
        renderTrialBalanceValidationGrid();
        return {
            previewHeaders: Array.from(document.querySelectorAll('#mappingPreviewHead th')).map(cell => cell.textContent),
            previewExtraValues: Array.from(document.querySelectorAll('#mappingPreviewBody td')).slice(8).map(cell => cell.textContent),
            validationHeaders: Array.from(document.querySelectorAll('#tbValidationHead th')).map(cell => cell.textContent),
            validationValues: Array.from(document.querySelectorAll('#tbValidationBody tr:first-child td')).map(cell => cell.textContent),
            validationExcelRow: document.querySelector('#tbValidationBody tr')?.dataset.excelRow
        };
    }""")

    assert result["previewHeaders"][-3:] == ["Start Date", "End Date", "Unnamed: 10"]
    assert result["previewExtraValues"] == ["2025-07-01", "2026-06-30", "3222222.22"]
    assert result["validationHeaders"] == [
        "Excel Row", "Account Code", "Account Name", "Classification",
        "Opening Debit", "Opening Credit", "Period Debit", "Period Credit",
        "Closing Debit", "Closing Credit", "Validation Issues",
    ]
    assert not {"2025-07-01", "2026-06-30", "3222222.22"}.intersection(result["validationValues"])
    assert result["validationExcelRow"] == "2"

    page.close()


def test_rollforward_differences_render_as_red_section_without_changing_source_row(browser):
    page = browser.new_page()
    page.set_content("""
        <table><thead id="tbValidationHead"></thead><tbody id="tbValidationBody"></tbody></table>
        <table><tbody id="availableAccountsTableBody"></tbody></table>
    """)
    page.add_script_tag(path=str(PROJECT_ROOT / "static" / "trial_balance.js"))

    result = page.evaluate("""() => {
        fileHeaders = [
            'Account Code', 'Account Name', 'Opening Debit', 'Opening Credit',
            'Period Debit', 'Period Credit', 'Closing Debit', 'Closing Credit'
        ];
        tbColumnMapping = {
            account_code_column: 'Account Code', account_name_column: 'Account Name',
            opening_dr_column: 'Opening Debit', opening_cr_column: 'Opening Credit',
            period_dr_column: 'Period Debit', period_cr_column: 'Period Credit',
            closing_dr_column: 'Closing Debit', closing_cr_column: 'Closing Credit'
        };
        fileAllRows = [['A1', 'Cash', '100', '0', '20', '0', '1234567.49', '0']];
        validationResult = {rollforward_differences: [{
            excel_row: 2, account_code: 'A1', account_name: 'Cash',
            closing_dr_adjustment: '2345650.5', closing_cr_adjustment: 0
        }]};
        validationIssueSnapshot = {errors: [], warnings: [{
            excel_row: 2, field: 'Roll-forward',
            message: 'Opening balance plus period movement does not equal closing balance within 0.01.'
        }]};
        autoClassificationByRow = {'2': 'leaf'};
        renderTrialBalanceValidationGrid();
        const sourceRow = document.querySelector('#tbValidationBody tr[data-excel-row="2"]');
        const originalClosingDebit = sourceRow.children[8].textContent;
        const differenceRow = document.querySelector('#tbValidationBody tr.tb-generated-difference');
        renderAvailableAccountsTable([], validationResult.rollforward_differences);
        return {
            originalClosingDebit,
            differenceLabel: differenceRow.children[2].textContent,
            differenceClosingDebit: differenceRow.children[8].textContent,
            validationSection: document.querySelector('#tbValidationBody tr.tb-difference-section')?.textContent,
            savedSection: document.querySelector('#availableAccountsTableBody tr.tb-difference-section')?.textContent,
            savedLabel: document.querySelector('#availableAccountsTableBody tr.tb-generated-difference td:nth-child(2)')?.textContent,
            savedDebit: document.querySelector('#availableAccountsTableBody tr.tb-generated-difference td:nth-child(9)')?.textContent
        };
    }""")

    assert result["originalClosingDebit"] == "1,234,567"
    assert result["differenceLabel"] == "Difference — Cash"
    assert result["differenceClosingDebit"] == "2,345,651"
    assert "System-generated differences (1)" in result["validationSection"]
    assert "System-generated differences (1)" in result["savedSection"]
    assert result["savedLabel"] == "Difference — Cash"
    assert result["savedDebit"] == "2345650.5"

    page.close()


def test_global_differences_allow_save_without_review_controls(browser):
    import re
    html=(PROJECT_ROOT / 'static' / 'trial_balance.html').read_text(encoding='utf-8')
    page=browser.new_page()
    page.set_content(re.sub(r'<script[\s\S]*?</script>', '', html))
    page.add_script_tag(path=str(PROJECT_ROOT / 'static' / 'trial_balance.js'))
    page.evaluate("""async () => {
        window.API_BASE=''; window.isClientSelected=()=>true;
        selectedFile=new File(['demo'],'tb.xlsx');
        periodStartInput.value='2026-01-01'; periodEndInput.value='2026-06-30';
        tbNoTotalRow.checked=true;
        fileHeaders=['Code','Name','OD','OC','PD','PC','CD','CC'];
        tbColumnMapping=Object.fromEntries(TB_MAPPING_FIELDS.map((field,index)=>[field.role,fileHeaders[index]]));
        fileAllRows=[['A','Cash',100,0,0,0,90,0]];
        tbValidationSheet.hidden=false;tbValidationSheet.classList.add('active');
        const result={valid:true,errors:[],issue_count:0,warning_count:2,account_count:1,no_total_row:true,
            auto_classification:{'2':'leaf'},checks:[{key:'account_codes',status:'pass'}],
            warnings:[{excel_row:2,field:'Roll-forward',message:'Row difference'},{field:'Footings',message:'Vertical difference'}],
            rollforward_difference_count:1,rollforward_differences:[{excel_row:2,account_code:'A',account_name:'Cash',closing_dr_adjustment:10}],
            global_adjustments:[{account_name:'Opening footing',global_adjustment:true,opening_cr_adjustment:'100',closing_cr_adjustment:'100'}]};
        window.fetch=async()=>({ok:true,json:async()=>result});
        await runTrialBalanceValidation();
    }""")
    assert page.locator('#uploadTrialBalanceBtn').is_enabled()
    assert page.get_by_role('button',name='Review global differences').count()==0
    assert page.locator('#tbFootingDialog').count()==0
    assert page.locator('.tb-generated-difference').count()==2
    status=page.locator('#tbValidationStatus').inner_text()
    assert 'ready to save' in status and 'global footing difference line(s)' in status
    assert 'Parent account-code' not in status and 'blocking issue' not in status
    assert not page.evaluate("buildTrialBalanceFormData().has('adjustment_confirmation')")
    page.evaluate("invalidateTrialBalanceValidation('Changed')")
    assert page.locator('#uploadTrialBalanceBtn').is_disabled()
    page.close()


def test_global_difference_preserves_all_six_saved_columns(browser):
    page = browser.new_page()
    page.set_content('<table><tbody id="availableAccountsTableBody"></tbody></table>')
    page.add_script_tag(path=str(PROJECT_ROOT / 'static' / 'trial_balance.js'))
    page.evaluate("""() => renderAvailableAccountsTable([], [{account_name: 'Opening footing',
        global_adjustment: true, opening_cr_adjustment: '100', closing_cr_adjustment: '100'}])""")
    cells = page.locator('.tb-generated-difference td').all_text_contents()
    assert cells[4:10] == ['', '100', '', '', '', '100']
    assert cells[2] == 'Unassigned global difference'
    assert cells[3] == ''
    page.close()


def test_saved_view_inline_edit_and_sticky_heading(browser):
    import re
    html = (PROJECT_ROOT / 'static' / 'trial_balance.html').read_text(encoding='utf-8')
    page = browser.new_page(viewport={'width': 900, 'height': 600})
    page.set_content(re.sub(r'<script[\s\S]*?</script>', '', html))
    page.add_script_tag(path=str(PROJECT_ROOT / 'static' / 'trial_balance.js'))
    page.evaluate("""() => {
        availableTrialBalancesSection.style.display='block';
        availableAccountsTableWrapper.style.display='block'; tbSavedStickyHead.hidden=false;
        window.savedCode=null;
        saveAvailableAccountCode=async (account, value) => { window.savedCode=value; return true; };
        renderAvailableAccountsTable(Array.from({length: 40}, (_, i) => ({id:i, account_code:'A'+i,
            account_name:'Cash', is_leaf:true})), []);
    }""")
    assert page.locator('#availableAccountsTableBody tr:first-child td').nth(2).inner_text() == 'Leaf'
    code = page.locator('#availableAccountsTableBody tr:first-child .tb-account-code-display')
    code.dblclick()
    editor = page.locator('#availableAccountsTableBody tr:first-child input')
    editor.fill('B0')
    editor.press('Enter')
    assert page.evaluate('window.savedCode') == 'B0'
    page.evaluate('availableAccountsTableWrapper.scrollLeft=200; window.scrollTo(0, 500)')
    page.wait_for_function("tbSavedStickyHead.querySelector('table').style.transform === `translateX(-${availableAccountsTableWrapper.scrollLeft}px)`")
    assert page.locator('#tbSavedStickyHead').evaluate('el => getComputedStyle(el).position') == 'sticky'
    assert page.locator('#availableAccountsTableWrapper').evaluate('el => getComputedStyle(el).maxHeight') == 'none'
    page.close()


def test_saved_classification_and_amount_widths_and_floating_scroll(browser):
    import re
    html = (PROJECT_ROOT / 'static' / 'trial_balance.html').read_text(encoding='utf-8')
    page = browser.new_page(viewport={'width': 900, 'height': 700})
    page.set_content(re.sub(r'<script[\s\S]*?</script>', '', html))
    page.add_script_tag(path=str(PROJECT_ROOT / 'static' / 'trial_balance.js'))
    page.evaluate("""() => {
        window.API_BASE=''; window.requests=[]; window.failOverride=false; window.getSelectedClientId=()=>42;
        const large='123456789012345678901234567890.00';
        const account={id:1,account_code:'A',account_name:'Cash',is_leaf:true,opening_dr:'1'};
        const totals=Object.fromEntries(['opening','period','closing'].flatMap(p=>['dr','cr'].map(s=>[p+'_'+s+'_column',large])));
        const controls={stale:false,draft_reconciliation:{original_totals:totals,adjustment_totals:totals,adjusted_totals:totals,pair_differences:{}}};
        window.fetch=async (url, options={}) => {
            window.requests.push({url,body:options.body});
            if (url.includes('/override')) {
                if (window.failOverride) return {ok:false,json:async()=>({detail:'Override rejected'})};
                account.is_leaf=JSON.parse(options.body).is_leaf;
                controls.stale=true;
                return {ok:true,json:async()=>account};
            }
            if (url.includes('/api/account-control-center/mapping')) return {ok:true,json:async()=>({client_id:42,upload:{id:1},accounts:[{account_code:'A',assignment:{financial_statement_category_id:7}}]})};
            if (url.includes('/api/account-control-center/categories')) return {ok:true,json:async()=>({categories:[{id:7,label:'Cash and cash equivalents',is_active:true}]})};
            return {ok:true,json:async()=>({accounts:[account],adjustments:[],validation_controls:controls})};
        };
        availableTrialBalancesSection.style.display='block';
        availableUploadsDropdown.innerHTML='<option value="1">One</option>';
        return loadAvailableUploadAccounts('1');
    }""")
    page.wait_for_function("document.querySelector('#availableAccountsTableBody tr:first-child td:nth-child(4)')?.textContent === 'Cash and cash equivalents'")
    classification = page.locator('#availableAccountsTableBody tr:first-child td').nth(2)
    widths = page.locator('#availableAccountsTableBody tr:first-child td.tb-amount-cell').evaluate_all('cells => cells.map(c => c.getBoundingClientRect().width)')
    assert len(widths) == 6 and max(widths) - min(widths) < 1
    assert page.locator('#availableAccountsTableBody tr:first-child td').nth(3).inner_text() == 'Cash and cash equivalents'
    assert page.locator('#availableAccountsTableBody .tb-draft-adjusted_totals td.tb-amount-cell').first.evaluate('el => el.scrollWidth <= el.clientWidth')
    bar = page.locator('.tb-saved-floating-scroll')
    assert bar.is_visible()
    assert page.locator('#availableAccountsTableWrapper').evaluate('el => getComputedStyle(el).scrollbarWidth') == 'none'
    bar.focus(); bar.press('End')
    page.wait_for_function('availableAccountsTableWrapper.scrollLeft > 0')
    page.wait_for_function("tbSavedStickyHead.querySelector('table').style.transform === `translateX(-${availableAccountsTableWrapper.scrollLeft}px)`")
    bar.press('Home')
    page.wait_for_function('availableAccountsTableWrapper.scrollLeft === 0')
    classification.dblclick()
    classification.locator('select').select_option('parent')
    classification.locator('select').press('Enter')
    page.wait_for_function("document.querySelector('#availableAccountsTableBody tr:first-child td:nth-child(3)').textContent.includes('Parent')")
    assert page.evaluate("JSON.parse(window.requests.find(r => r.url.includes('/override')).body).is_leaf") is False
    assert 'changed after validation' in page.locator('#tbSavedValidationControls').inner_text()
    page.evaluate('window.failOverride=true')
    classification.dblclick()
    classification.locator('select').select_option('leaf')
    classification.locator('select').press('Enter')
    page.wait_for_function("document.querySelector('#tbSavedStatus').textContent.includes('Override rejected')")
    assert 'Parent' in classification.inner_text()
    page.locator('#closeAvailableTrialBalancesBtn').click()
    assert bar.is_hidden()
    page.close()


def test_saved_trial_balance_category_lookup_and_search_filter(browser):
    import re
    html = (PROJECT_ROOT / 'static' / 'trial_balance.html').read_text(encoding='utf-8')
    page = browser.new_page()
    page.set_content(re.sub(r'<script[\s\S]*?</script>', '', html))
    page.add_script_tag(path=str(PROJECT_ROOT / 'static' / 'trial_balance.js'))
    page.evaluate("""() => {
        window.API_BASE=''; window.getSelectedClientId=()=>42;
        const accounts=[
            {id:1,account_code:'100',account_name:'Cash',is_leaf:true,opening_dr:'10',closing_dr:'12'},
            {id:2,account_code:'200',account_name:'Unassigned account',is_leaf:true,opening_dr:'20',closing_dr:'22'},
            {id:3,account_code:'300',account_name:'Trade receivables',is_leaf:true,opening_dr:'30',closing_dr:'32'}
        ];
        window.fetch=async url => {
            if (url.includes('/api/trial-balance/accounts')) return {ok:true,json:async()=>({accounts,adjustments:[],validation_controls:{}})};
            if (url.includes('/api/account-control-center/mapping')) return {ok:true,json:async()=>({client_id:42,upload:{id:59},accounts:[
                {account_code:'100',assignment:{financial_statement_category_id:7}},
                {account_code:'200',assignment:{financial_statement_category_id:null}},
                {account_code:'300',assignment:{financial_statement_category_id:8}}
            ]})};
            return {ok:true,json:async()=>({categories:[
                {id:7,label:'Cash and cash equivalents',is_active:true},
                {id:8,label:'Trade receivables',is_active:true},
                {id:9,label:'Retired category',is_active:false}
            ]})};
        };
        availableTrialBalancesSection.style.display='block';
        availableUploadsDropdown.innerHTML='<option value="59">TB</option>';
        availableUploadsDropdown.value='59';
        return loadAvailableUploadAccounts('59');
    }""")
    page.wait_for_function("document.querySelector('#availableAccountsTableBody tr:first-child td:nth-child(4)')?.textContent === 'Cash and cash equivalents'")
    headers = page.locator('#availableAccountsTableWrapper thead th').all_text_contents()
    assert headers[:4] == ['Code', 'Name', 'Classification', 'Category']
    assert page.locator('#availableAccountsTableBody tr:first-child td').nth(2).inner_text() == 'Leaf'
    assert page.locator('#availableAccountsTableBody tr:first-child td').nth(3).inner_text() == 'Cash and cash equivalents'
    assert page.locator('#availableAccountsTableBody tr:nth-child(2) td').nth(3).inner_text() == 'Unassigned'
    assert page.locator('#availableAccountsTableBody tr:first-child td:nth-child(4) input, #availableAccountsTableBody tr:first-child td:nth-child(4) select').count() == 0
    assert page.locator('#tbSavedCategorySearch').is_enabled()
    options = page.locator('#tbSavedCategoryOptions option').evaluate_all('items => items.map(item => item.value)')
    assert options == ['Cash and cash equivalents', 'Trade receivables', 'Unassigned']
    category_filter = page.locator('#tbSavedCategorySearch')
    category_filter.fill('receiv')
    assert page.locator('#availableAccountsTableBody tr[data-category-filter]:visible').count() == 1
    assert page.locator('#availableAccountsTableBody tr[data-category-filter]:visible td').nth(1).inner_text() == 'Trade receivables'
    category_filter.fill('Unassigned')
    assert page.locator('#availableAccountsTableBody tr[data-category-filter]:visible').count() == 1
    category_filter.fill('not a category')
    assert page.locator('#tbSavedCategoryNoMatches').is_visible()
    category_filter.fill('')
    assert page.locator('#availableAccountsTableBody tr[data-category-filter]:visible').count() == 3
    assert page.locator('#availableAccountsTableBody tr:first-child td.tb-amount-cell').first.inner_text() == '10'
    page.close()


def test_saved_trial_balance_remains_available_when_category_lookup_fails(browser):
    page = browser.new_page()
    page.set_content('<div id="availableAccountsTableWrapper"><table><thead><tr><th>Code</th></tr></thead><tbody id="availableAccountsTableBody"></tbody></table></div><div id="tbSavedStickyHead"></div><select id="availableUploadsDropdown"><option value="59">TB</option></select><input id="tbSavedCategorySearch"><datalist id="tbSavedCategoryOptions"></datalist><p id="tbSavedCategoryNoMatches" hidden></p>')
    page.add_script_tag(path=str(PROJECT_ROOT / 'static' / 'trial_balance.js'))
    page.evaluate("""() => {
        window.API_BASE=''; window.getSelectedClientId=()=>42;
        window.fetch=async url => url.includes('/api/trial-balance/accounts')
            ? {ok:true,json:async()=>({accounts:[{id:1,account_code:'A',account_name:'Cash',is_leaf:true,closing_dr:'50'}],adjustments:[],validation_controls:{}})}
            : {ok:false,json:async()=>({detail:'ACC unavailable'})};
        availableUploadsDropdown.value='59';
        return loadAvailableUploadAccounts('59');
    }""")
    page.wait_for_function("document.querySelector('#tbSavedCategoryNoMatches')?.textContent.includes('categories are unavailable')")
    assert page.locator('#availableAccountsTableWrapper tbody tr').count() == 1
    assert page.locator('#availableAccountsTableWrapper tbody tr:first-child td').nth(3).inner_text() == 'Unavailable'
    assert page.locator('#availableAccountsTableWrapper tbody tr:first-child td.tb-amount-cell').nth(4).inner_text() == '50'
    assert page.locator('#tbSavedCategorySearch').is_disabled()
    assert 'categories are unavailable' in page.locator('#tbSavedCategoryNoMatches').inner_text()
    page.close()


def test_manual_total_row_mapping_preview_gating_and_file_reset(browser):
    import re
    html = (PROJECT_ROOT / 'static' / 'trial_balance.html').read_text(encoding='utf-8')
    page = browser.new_page(viewport={'width': 1280, 'height': 1000})
    page.set_content(re.sub(r'<script[\s\S]*?</script>', '', html))
    page.add_script_tag(path=str(PROJECT_ROOT / 'static' / 'trial_balance.js'))
    page.evaluate("""() => {
        window.isClientSelected = () => true;
        selectedFile = new File(['demo'], 'tb.xlsx');
        periodStartInput.value = '2026-01-01'; periodEndInput.value = '2026-06-30';
        fileHeaders = ['Code', 'Name', 'OD', 'OC', 'PD', 'PC', 'CD', 'CC'];
        tbColumnMapping = Object.fromEntries(TB_MAPPING_FIELDS.map((f,i) => [f.role,fileHeaders[i]]));
        fileAllRows = [['A', 'Debit', 10, 0, 0, 0, 10, 0], ['B', 'Credit', 0, 10, 0, 0, 0, 10],
                       ['', 'ERP Total', 10, 10, 0, 0, 10, 10]];
        mappingSection.style.display = 'block';
        renderMappingDropdowns(); updateClientDependentUI();
    }""")
    assert page.locator('#validateTrialBalanceBtn').is_disabled()
    page.locator('#tbTotalExcelRow').fill('4')
    assert page.locator('#validateTrialBalanceBtn').is_enabled()
    assert 'Row 4 · ERP Total' in page.locator('#tbTotalRowPreviewLabel').inner_text()
    page.locator('#tbTotalRowPreviewLabel').click()
    assert page.locator('#tbTotalRowAmounts').all_text_contents() == ['OpeningDr 10Cr 10PeriodDr 0Cr 0ClosingDr 10Cr 10']
    assert page.evaluate("buildTrialBalanceFormData().get('total_excel_row')") == '4'
    assert page.evaluate("buildTrialBalanceFormData().get('no_total_row')") == 'false'
    page.evaluate("validationResult = {valid:true}; validationDirty = false;")
    page.locator('#tbNoTotalRow').check()
    assert page.locator('#tbTotalExcelRow').is_disabled()
    assert page.locator('#tbTotalRowPreview').is_hidden()
    assert page.evaluate("buildTrialBalanceFormData().get('total_excel_row')") is None
    assert page.evaluate("buildTrialBalanceFormData().get('no_total_row')") == 'true'
    assert page.evaluate('validationDirty && !validationResult')
    page.locator('#tbNoTotalRow').uncheck()
    page.locator('#tbTotalExcelRow').fill('99')
    assert page.locator('#validateTrialBalanceBtn').is_disabled()
    assert page.locator('#tbTotalRowError').is_visible()
    page.evaluate('resetColumnMapping()')
    assert page.locator('#tbTotalExcelRow').input_value() == ''
    assert not page.locator('#tbNoTotalRow').is_checked()
    page.close()


def test_erp_control_footer_aligns_six_columns_and_keeps_original_leaf_totals(browser):
    page = browser.new_page()
    page.set_content("""
        <div id="tbValidationControls"></div>
        <table><thead id="tbValidationHead"></thead><tbody id="tbValidationBody"></tbody></table>
    """)
    page.add_script_tag(path=str(PROJECT_ROOT / 'static' / 'trial_balance.js'))
    result = page.evaluate("""() => {
        fileHeaders = ['Name', 'CC', 'Code', 'PD', 'OD', 'CD', 'OC', 'PC', 'Unmapped'];
        tbColumnMapping = {account_code_column:'Code', account_name_column:'Name',
            opening_dr_column:'OD', opening_cr_column:'OC', period_dr_column:'PD',
            period_cr_column:'PC', closing_dr_column:'CD', closing_cr_column:'CC'};
        fileAllRows = [['Debit',0,'A',20,100,110,0,0,'extra'], ['Credit',110,'B',0,0,0,100,20,'extra'],
                       ['ERP footer',110,'Total',20,100,110,100,20,'extra']];
        autoClassificationByRow = {'2':'leaf','3':'leaf'};
        validationResult = {
            no_total_row:false, source_total_rows:[4],
            source_leaf_totals:{opening_dr_column:'100',opening_cr_column:'100',period_dr_column:'20',
                period_cr_column:'20',closing_dr_column:'110',closing_cr_column:'110'},
            source_total_comparison:['Opening Dr','Opening Cr','Period Dr','Period Cr','Closing Dr','Closing Cr']
                .map((field,i) => ({field,source_total:i<2?'100':i<4?'20':'110',difference:'0.00'})),
            checks:[{key:'account_codes',label:'Account codes and leaf selection',status:'pass'}],
            rollforward_differences:[{excel_row:2, account_code:'A', account_name:'Debit', closing_dr_adjustment:10}]
        };
        renderTrialBalanceControls(validationResult);
        renderTrialBalanceValidationGrid();
        const headers=Array.from(document.querySelectorAll('#tbValidationHead th')).map(c=>c.textContent);
        const cells=selector=>Array.from(document.querySelectorAll(selector+' td')).map(c=>c.textContent);
        const values=selector=>['OD','OC','PD','PC','CD','CC'].map(h=>cells(selector)[headers.indexOf(h)]);
        const matched={leaf:values('.tb-control-leaf'), erp:values('.tb-control-erp'),
            difference:values('.tb-control-difference'),
            label:cells('.tb-control-erp')[headers.indexOf('Name')],
            lastRow:document.querySelector('#tbValidationBody tr:last-child').className,
            banner:document.querySelector('#tbValidationControls').textContent,
            zeroCells:document.querySelectorAll('.tb-footing-zero').length,
            footerEditors:document.querySelectorAll('.tb-control-total input, .tb-control-total select').length,
            exact:formatTrialBalanceControlAmount('9007199254740993.01')};
        validationResult.source_total_comparison[0].difference='0.004';
        renderTrialBalanceValidationGrid();
        const nonzero={value:values('.tb-control-difference')[0],red:document.querySelectorAll('.tb-footing-nonzero').length};
        validationResult.checks[0].status='fail'; validationResult.source_total_comparison=[];
        renderTrialBalanceValidationGrid();
        const pending=values('.tb-control-difference');
        validationResult.no_total_row=true; validationResult.source_total_rows=[];
        renderTrialBalanceValidationGrid();
        return {matched,nonzero,pending,notApplicable:values('.tb-control-difference')};
    }""")
    assert result['matched']['leaf'] == ['100', '100', '20', '20', '110', '110']
    assert result['matched']['erp'] == result['matched']['leaf']
    assert result['matched']['difference'] == ['0'] * 6
    assert result['matched']['label'] == 'Client ERP total'
    assert 'tb-control-difference' in result['matched']['lastRow']
    assert result['matched']['banner'] == ''
    assert result['matched']['zeroCells'] == 6
    assert result['matched']['footerEditors'] == 0
    assert result['matched']['exact'] == '9,007,199,254,740,993.01'
    assert result['nonzero'] == {'value': '0', 'red': 0}
    assert result['pending'] == ['Pending'] * 6
    assert result['notApplicable'] == ['N/A'] * 6
    page.close()


def test_control_rounding_and_difference_colours_follow_server_tolerance(browser):
    page=browser.new_page()
    page.set_content('<table><thead id="tbValidationHead"></thead><tbody id="tbValidationBody"></tbody></table>')
    page.add_script_tag(path=str(PROJECT_ROOT / 'static' / 'trial_balance.js'))
    result=page.evaluate("""() => {
        fileHeaders=['Code','Name','OD','OC','PD','PC','CD','CC'];
        tbColumnMapping=Object.fromEntries(TB_MAPPING_FIELDS.map((field,index)=>[field.role,fileHeaders[index]]));
        fileAllRows=[['A','Debit',1,0,0,0,1,0],['B','Credit',0,1,0,0,0,1],['','Total',1,1,0,0,1,1]];
        const differences=['-8.97E-8','-0.004','0.01','-0.01','0.02','0.0100000000000000000001'];
        validationResult={tolerance:0.01,source_total_rows:[4],source_leaf_totals:{},
            checks:[{key:'account_codes',status:'pass'}],
            source_total_comparison:['Opening Dr','Opening Cr','Period Dr','Period Cr','Closing Dr','Closing Cr']
                .map((field,i)=>({field,source_total:'1',difference:differences[i],within_tolerance:i<4}))};
        const original=JSON.stringify(validationResult);
        renderTrialBalanceValidationGrid();
        return {amounts:['1234567890.1970003','9007199254740993.015','-1.005','9.995e2','-8.97E-8']
                    .map(value=>formatTrialBalanceControlAmount(value)),
            wholeAmounts:['2345650.49','2345650.50','1.499','-1.5','-0.004','9007199254740993.5','9.995e2']
                    .map(value=>formatTrialBalanceControlAmount(value,0)),
            cells:Array.from(document.querySelectorAll('.tb-control-difference .tb-validation-amount'))
                .map(cell=>({text:cell.textContent,red:cell.classList.contains('tb-footing-nonzero'),title:cell.title})),
            unchanged:JSON.stringify(validationResult)===original};
    }""")
    assert result['amounts']==['1,234,567,890.20','9,007,199,254,740,993.02','-1.01','999.50','0.00']
    assert result['wholeAmounts']==['2,345,650','2,345,651','1','-2','0','9,007,199,254,740,994','1,000']
    assert [cell['text'] for cell in result['cells']]==['0','0','0','0','<1','<1']
    assert [cell['red'] for cell in result['cells']]==[False,False,False,False,True,True]
    assert '0.0100000000000000000001' in result['cells'][-1]['title']
    assert result['unchanged']
    page.close()


@pytest.mark.parametrize('width',[1920,1280,900])
@pytest.mark.parametrize('total',['1526512467.1970003','9007199254740993.015'])
def test_control_totals_fit_and_scrolling_keeps_headers_aligned(browser,width,total):
    import re
    html=(PROJECT_ROOT / 'static' / 'trial_balance.html').read_text(encoding='utf-8')
    page=browser.new_page(viewport={'width':width,'height':1080})
    page.set_content(re.sub(r'<script[\s\S]*?</script>','',html))
    page.add_style_tag(path=str(PROJECT_ROOT / 'static' / 'style.css'))
    page.add_script_tag(path=str(PROJECT_ROOT / 'static' / 'trial_balance.js'))
    page.evaluate("""total => {
        fileHeaders=['Code','Name','OD','OC','PD','PC','CD','CC'];
        tbColumnMapping=Object.fromEntries(TB_MAPPING_FIELDS.map((field,index)=>[field.role,fileHeaders[index]]));
        fileAllRows=[['A','Debit','2345650.49',0,'-1000.5',0,1,0],['B','Credit',0,1,0,0,0,1],['','Total',1,1,0,0,1,1]];
        window.formatAmount=()=> 'unrelated global format';
        autoClassificationByRow={'2':'leaf','3':'leaf'};
        const roles=['opening_dr_column','opening_cr_column','period_dr_column','period_cr_column','closing_dr_column','closing_cr_column'];
        validationResult={source_total_rows:[4],source_leaf_totals:Object.fromEntries(roles.map(role=>[role,total])),
            checks:[{key:'account_codes',status:'pass'}],source_total_comparison:
            ['Opening Dr','Opening Cr','Period Dr','Period Cr','Closing Dr','Closing Cr']
            .map(field=>({field,source_total:total,difference:'-2.97E-7',within_tolerance:true})),
            draft_reconciliation:{adjustment_totals:Object.fromEntries(roles.map(role=>[role,'0'])),
                adjusted_totals:Object.fromEntries(roles.map(role=>[role,total])),
                pair_differences:Object.fromEntries(['opening','period','closing'].map(period=>[period,{difference:'0',within_tolerance:true}]))}};
        const original=JSON.stringify(fileAllRows);
        tbValidationSheet.hidden=false;tbValidationSheet.classList.add('active');renderTrialBalanceValidationGrid();
        window.sourceUnchanged=JSON.stringify(fileAllRows)===original;
        tbValidationTableWrap.scrollLeft=250;
    }""",total)
    page.wait_for_function("document.querySelector('#tbValidationStickyHead table').style.transform === `translateX(-${tbValidationTableWrap.scrollLeft}px)`")
    measurements=page.evaluate("""() => {
        const cells=Array.from(document.querySelectorAll('#tbValidationBody tr[data-excel-row] td, .tb-control-total td, .tb-draft-total td'));
        const headers=Array.from(document.querySelectorAll('#tbValidationStickyHead th'));
        return cells.map((cell,i)=>{
            const range=document.createRange();range.selectNodeContents(cell);
            const style=getComputedStyle(cell);
            return {amount:cell.classList.contains('tb-validation-amount'),
                fits:range.getBoundingClientRect().width<=cell.clientWidth-parseFloat(style.paddingLeft)-parseFloat(style.paddingRight)+1,
                aligned:Math.abs(cell.getBoundingClientRect().left-headers[cell.cellIndex].getBoundingClientRect().left)<1,
                right:style.textAlign==='right'};
        });
    }""")
    assert all(cell['aligned'] for cell in measurements)
    assert all(cell['fits'] and cell['right'] for cell in measurements if cell['amount'])
    amount_widths=page.locator('#tbValidationBody tr[data-excel-row="2"] .tb-validation-amount').evaluate_all(
        'cells=>cells.map(cell=>cell.getBoundingClientRect().width)')
    assert len(amount_widths)==6 and max(amount_widths)-min(amount_widths)<1
    if total.startswith('1526'):
        assert max(amount_widths)<130  # Real client footings no longer reserve 160px per amount.
    assert page.locator('#tbValidationBody tr[data-excel-row="2"] .tb-validation-amount').all_text_contents()[:3]==['2,345,650','0','-1,001']
    assert page.locator('#tbValidationBody tr[data-excel-row="2"] .tb-validation-amount').first.get_attribute('title')=='Unrounded amount: 2345650.49'
    assert page.evaluate('window.sourceUnchanged')
    page.close()


def test_draft_footer_preserves_erp_values_and_shows_balanced_totals_in_both_views(browser):
    page=browser.new_page()
    page.set_content('<table><thead id="tbValidationHead"></thead><tbody id="tbValidationBody"></tbody></table>'
                     '<table><tbody id="availableAccountsTableBody"></tbody></table>')
    page.add_script_tag(path=str(PROJECT_ROOT / 'static' / 'trial_balance.js'))
    result=page.evaluate("""() => {
        fileHeaders=['Name','CC','Code','PD','OD','CD','OC','PC'];
        tbColumnMapping={account_code_column:'Code',account_name_column:'Name',opening_dr_column:'OD',opening_cr_column:'OC',
            period_dr_column:'PD',period_cr_column:'PC',closing_dr_column:'CD',closing_cr_column:'CC'};
        fileAllRows=[['Debit',0,'A',100,0,90,0,0],['Credit',120,'B',0,0,0,0,120],['Total',120,'',100,0,90,0,120]];
        const roles=['opening_dr_column','opening_cr_column','period_dr_column','period_cr_column','closing_dr_column','closing_cr_column'];
        const totals=values=>Object.fromEntries(roles.map((role,i)=>[role,String(values[i])]));
        validationResult={source_total_rows:[4],no_total_row:false,erp_unbalanced:true,checks:[{key:'account_codes',status:'pass'}],
            source_leaf_totals:totals([0,0,100,120,90,120]),source_total_comparison:
            ['Opening Dr','Opening Cr','Period Dr','Period Cr','Closing Dr','Closing Cr']
                .map((field,i)=>({field,source_total:String([0,0,100,120,90,120][i]),difference:'0',within_tolerance:true})),
            draft_reconciliation:{original_totals:totals([0,0,100,120,90,120]),adjustment_totals:totals([0,0,20,0,30,0]),
                adjusted_totals:totals([0,0,120,120,120,120]),pair_differences:
                Object.fromEntries(['opening','period','closing'].map(p=>[p,{difference:'0',within_tolerance:true}]))}};
        renderTrialBalanceValidationGrid();
        const headers=Array.from(document.querySelectorAll('#tbValidationHead th')).map(c=>c.textContent);
        const values=selector=>['OD','OC','PD','PC','CD','CC'].map(h=>document.querySelector(selector).children[headers.indexOf(h)].textContent);
        const original={erp:values('#tbValidationBody .tb-control-erp'),
            adjustments:values('#tbValidationBody .tb-draft-adjustment_totals'),
            adjusted:values('#tbValidationBody .tb-draft-adjusted_totals'),
            pairs:values('#tbValidationBody .tb-draft-pairs'),
            warning:document.querySelector('#tbValidationBody .tb-control-difference').nextElementSibling.textContent};
        renderAvailableAccountsTable([],[],validationResult);
        const saved=Array.from(document.querySelectorAll('#availableAccountsTableBody .tb-draft-adjusted_totals td')).slice(4,10).map(c=>c.textContent);
        validationResult.draft_reconciliation.pair_differences.opening={difference:'-0.02',within_tolerance:false};
        renderTrialBalanceValidationGrid();
        const smallPair={text:values('#tbValidationBody .tb-draft-pairs')[0],
            red:document.querySelector('#tbValidationBody .tb-draft-pairs .tb-footing-nonzero')?.title};
        validationResult.draft_reconciliation=null;
        renderTrialBalanceValidationGrid();
        const pending=values('#tbValidationBody .tb-draft-pairs');
        validationResult.erp_unbalanced=false;
        renderTrialBalanceValidationGrid();
        return {original,saved,smallPair,pending,warnings:document.querySelectorAll('#tbValidationBody .tb-erp-warning').length};
    }""")
    assert result['original']['erp']==['0','0','100','120','90','120']
    assert result['original']['adjustments']==['0','0','20','0','30','0']
    assert result['original']['adjusted']==['0','0','120','120','120','120']
    assert result['original']['pairs']==['0','','0','','0','']
    assert result['original']['warning']=='ERP imbalance: query pending.'
    assert result['saved']==['0.00','0.00','120.00','120.00','120.00','120.00']
    assert result['smallPair']=={'text':'<1','red':'Unrounded debit minus credit: -0.02'}
    assert result['pending']==['Pending','','Pending','','Pending','']
    assert result['warnings']==0
    page.close()


def test_validation_floating_scrollbar_tracks_table_and_supports_pointer_keyboard(browser):
    import re
    html=(PROJECT_ROOT/'static'/'trial_balance.html').read_text(encoding='utf-8')
    page=browser.new_page(viewport={'width':1000,'height':900})
    page.set_content(re.sub(r'<script[\s\S]*?</script>','',html))
    page.add_style_tag(path=str(PROJECT_ROOT/'static'/'style.css'))
    page.add_script_tag(path=str(PROJECT_ROOT/'static'/'trial_balance.js'))
    page.evaluate("""() => {
        fileHeaders=['Code','Name','OD','OC','PD','PC','CD','CC'];
        tbColumnMapping=Object.fromEntries(TB_MAPPING_FIELDS.map((field,index)=>[field.role,fileHeaders[index]]));
        fileAllRows=[['A','Debit','123456789012345','0','0','0','123456789012345','0'],
                     ['B','Credit','0','123456789012345','0','0','0','123456789012345']];
        autoClassificationByRow={'2':'leaf','3':'leaf'};
        validationResult={no_total_row:true,source_total_rows:[],checks:[{key:'account_codes',status:'pass'}],
            draft_reconciliation:{adjustment_totals:{},adjusted_totals:{},pair_differences:{}}};
        tbValidationSheet.hidden=false;tbValidationSheet.classList.add('active');
        renderTrialBalanceValidationGrid();
    }""")
    bar=page.locator('.tb-validation-floating-scroll')
    assert bar.is_visible()
    assert page.locator('#tbValidationTableWrap').evaluate("el=>getComputedStyle(el).scrollbarWidth")=='none'

    bar.focus()
    bar.press('End')
    page.wait_for_function("tbValidationTableWrap.scrollLeft > 0 && document.querySelector('.tb-validation-universal-scrollbar').getAttribute('aria-valuenow') === String(Math.round(tbValidationTableWrap.scrollLeft))")
    assert bar.get_attribute('aria-valuenow')==page.locator('#tbValidationTableWrap').evaluate('el=>String(Math.round(el.scrollLeft))')
    assert page.locator('#tbValidationStickyHead table').evaluate("el=>el.style.transform")==page.locator('#tbValidationTableWrap').evaluate("el=>`translateX(-${el.scrollLeft}px)`")

    bar.press('Home')
    page.wait_for_function("tbValidationTableWrap.scrollLeft === 0")
    track=bar.locator('.tb-validation-universal-scrollbar-track')
    box=track.bounding_box()
    page.mouse.click(box['x']+box['width']*.8,box['y']+box['height']/2)
    page.wait_for_function("tbValidationTableWrap.scrollLeft > 0")
    assert float(bar.get_attribute('aria-valuenow'))>0

    thumb=bar.locator('.tb-validation-universal-scrollbar-thumb')
    thumb_box=thumb.bounding_box()
    page.mouse.move(thumb_box['x']+thumb_box['width']/2,thumb_box['y']+3)
    page.mouse.down()
    page.mouse.move(box['x']+box['width']-8,thumb_box['y']+3,steps=4)
    page.mouse.up()
    page.wait_for_function("tbValidationTableWrap.scrollLeft >= tbValidationTableWrap.scrollWidth - tbValidationTableWrap.clientWidth - 2")

    page.evaluate("() => { const spacer=document.createElement('div'); spacer.style.height='1400px'; document.body.appendChild(spacer); window.scrollTo(0,document.body.scrollHeight); }")
    page.wait_for_function("document.querySelector('.tb-validation-universal-scrollbar').hidden")
    page.evaluate('window.scrollTo(0,0)')
    page.wait_for_function("!document.querySelector('.tb-validation-universal-scrollbar').hidden")
    page.close()
