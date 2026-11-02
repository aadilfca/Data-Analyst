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
    page.add_script_tag(path=str(PROJECT_ROOT / 'static' / 'account_assignment_shared.js'))
    page.add_script_tag(path=str(PROJECT_ROOT / 'static' / 'financial_statement_workspace.js'))
    page.add_script_tag(path=str(PROJECT_ROOT / "static" / "cashflow_workspace.js"))
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
    page.add_script_tag(path=str(PROJECT_ROOT / 'static' / 'account_assignment_shared.js'))
    page.add_script_tag(path=str(PROJECT_ROOT / 'static' / 'financial_statement_workspace.js'))
    page.add_script_tag(path=str(PROJECT_ROOT / "static" / "cashflow_workspace.js"))
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
            savedDebit: document.querySelector('#availableAccountsTableBody tr.tb-generated-difference td:nth-child(10)')?.textContent
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
    page.add_script_tag(path=str(PROJECT_ROOT / 'static' / 'account_assignment_shared.js'))
    page.add_script_tag(path=str(PROJECT_ROOT / 'static' / 'financial_statement_workspace.js'))
    page.add_script_tag(path=str(PROJECT_ROOT / "static" / "cashflow_workspace.js"))
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
    page.add_script_tag(path=str(PROJECT_ROOT / 'static' / 'account_assignment_shared.js'))
    page.add_script_tag(path=str(PROJECT_ROOT / 'static' / 'financial_statement_workspace.js'))
    page.add_script_tag(path=str(PROJECT_ROOT / "static" / "cashflow_workspace.js"))
    page.add_script_tag(path=str(PROJECT_ROOT / 'static' / 'trial_balance.js'))
    page.evaluate("""() => renderAvailableAccountsTable([], [{account_name: 'Opening footing',
        global_adjustment: true, opening_cr_adjustment: '100', closing_cr_adjustment: '100'}])""")
    cells = page.locator('.tb-generated-difference td').all_text_contents()
    assert cells[5:11] == ['', '100', '', '', '', '100']
    assert cells[2] == 'Unassigned global difference'
    assert cells[3] == ''
    assert cells[4] == ''
    page.close()


def test_saved_view_inline_edit_and_sticky_heading(browser):
    import re
    html = (PROJECT_ROOT / 'static' / 'trial_balance.html').read_text(encoding='utf-8')
    page = browser.new_page(viewport={'width': 900, 'height': 600})
    page.set_content(re.sub(r'<script[\s\S]*?</script>', '', html))
    page.add_script_tag(path=str(PROJECT_ROOT / 'static' / 'account_assignment_shared.js'))
    page.add_script_tag(path=str(PROJECT_ROOT / 'static' / 'financial_statement_workspace.js'))
    page.add_script_tag(path=str(PROJECT_ROOT / "static" / "cashflow_workspace.js"))
    page.add_script_tag(path=str(PROJECT_ROOT / 'static' / 'trial_balance.js'))
    page.evaluate("""() => {
        availableTrialBalancesSection.style.display='block';
        availableAccountsTableWrapper.style.display='block'; tbSavedStickyHead.hidden=false;
        window.savedCode=null;
        window.getSelectedClientId=()=>1;
        renderAvailableAccountsTable(Array.from({length: 40}, (_, i) => ({id:i, account_code:'A'+i,
            account_name:'Cash', is_leaf:true})), []);
    }""")
    assert page.locator('#availableAccountsTableBody tr:first-child td').nth(2).inner_text() == 'Leaf'
    code = page.locator('#availableAccountsTableBody tr:first-child .tb-account-code-display')
    code.dblclick()
    editor = page.locator('#availableAccountsTableBody tr:first-child input')
    editor.fill('B0')
    editor.press('Enter')
    assert page.evaluate('tbPendingCodeCorrections.get(0).account_code') == 'B0'
    assert page.evaluate('window.savedCode') is None
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
    page.add_script_tag(path=str(PROJECT_ROOT / 'static' / 'account_assignment_shared.js'))
    page.add_script_tag(path=str(PROJECT_ROOT / 'static' / 'financial_statement_workspace.js'))
    page.add_script_tag(path=str(PROJECT_ROOT / "static" / "cashflow_workspace.js"))
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
            if (url.includes('/api/account-control-center/mapping')) return {ok:true,json:async()=>({client_id:42,upload:{id:1},accounts:[{account_code:'A',assignment:{financial_statement_category_id:7,note_category_id:11}}]})};
            if (url.includes('category_type=note')) return {ok:true,json:async()=>({categories:[{id:11,label:'Cash note',is_active:true}]})};
            if (url.includes('/api/account-control-center/categories')) return {ok:true,json:async()=>({categories:[{id:7,label:'Cash and cash equivalents',is_active:true}]})};
            return {ok:true,json:async()=>({accounts:[account],adjustments:[],validation_controls:controls})};
        };
        availableTrialBalancesSection.style.display='block';
        availableUploadsDropdown.innerHTML='<option value="1">One</option>';
        return loadAvailableUploadAccounts('1');
    }""")
    page.wait_for_function("document.querySelector('#availableAccountsTableBody tr:first-child td:nth-child(4)')?.textContent === 'Cash and cash equivalents'")
    page.wait_for_function("document.querySelector('#availableAccountsTableBody tr:first-child td:nth-child(5)')?.textContent === 'Cash note'")
    classification = page.locator('#availableAccountsTableBody tr:first-child td').nth(2)
    widths = page.locator('#availableAccountsTableBody tr:first-child td.tb-amount-cell').evaluate_all('cells => cells.map(c => c.getBoundingClientRect().width)')
    assert len(widths) == 6 and max(widths) - min(widths) < 1
    assert page.locator('#availableAccountsTableBody tr:first-child td').nth(3).inner_text() == 'Cash and cash equivalents'
    assert page.locator('#availableAccountsTableBody tr:first-child td').nth(4).inner_text() == 'Cash note'
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
    page.add_script_tag(path=str(PROJECT_ROOT / 'static' / 'account_assignment_shared.js'))
    page.add_script_tag(path=str(PROJECT_ROOT / 'static' / 'financial_statement_workspace.js'))
    page.add_script_tag(path=str(PROJECT_ROOT / "static" / "cashflow_workspace.js"))
    page.add_script_tag(path=str(PROJECT_ROOT / 'static' / 'trial_balance.js'))
    page.evaluate("""() => {
        window.API_BASE=''; window.getSelectedClientId=()=>42;
        const accounts=[
            {id:1,account_code:'100',account_name:'Cash',is_leaf:true,opening_dr:'10',opening_cr:'0',period_dr:'5',period_cr:'1',closing_dr:'12',closing_cr:'0'},
            {id:2,account_code:'200',account_name:'Unassigned account',is_leaf:true,opening_dr:'20',opening_cr:'2',period_dr:'4',period_cr:'1',closing_dr:'23',closing_cr:'3'},
            {id:3,account_code:'300',account_name:'Trade receivables',is_leaf:true,opening_dr:'30',opening_cr:'2',period_dr:'3',period_cr:'1',closing_dr:'32',closing_cr:'4'}
        ];
        const totalRoles=['opening','period','closing'].flatMap(period=>['dr','cr'].map(side=>[period+'_'+side+'_column','0']));
        const zeroTotals=Object.fromEntries(totalRoles);
        const controls={draft_reconciliation:{original_totals:zeroTotals,adjustment_totals:zeroTotals,adjusted_totals:zeroTotals,
            pair_differences:Object.fromEntries(['opening','period','closing'].map(period=>[period,{difference:'0',within_tolerance:true}]))}};
        window.fetch=async url => {
            if (url.includes('/api/trial-balance/accounts')) return {ok:true,json:async()=>({accounts,adjustments:[],validation_controls:controls})};
            if (url.includes('/api/account-control-center/mapping')) return {ok:true,json:async()=>({client_id:42,upload:{id:59},accounts:[
                {account_code:'100',assignment:{financial_statement_category_id:7,note_category_id:21}},
                {account_code:'200',assignment:{financial_statement_category_id:null,note_category_id:null}},
                {account_code:'300',assignment:{financial_statement_category_id:8,note_category_id:23}}
            ]})};
            if (url.includes('category_type=note')) return {ok:true,json:async()=>({categories:[
                {id:21,label:'Cash note',is_active:true},
                {id:23,label:'Receivables note',is_active:true},
                {id:24,label:'Inactive note',is_active:false}
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
        deleteUploadBtn.style.display='inline-block';
        return loadAvailableUploadAccounts('59');
    }""")
    page.wait_for_function("document.querySelector('#availableAccountsTableBody tr:first-child td:nth-child(4)')?.textContent === 'Cash and cash equivalents' && document.querySelector('#availableAccountsTableBody tr:first-child td:nth-child(5)')?.textContent === 'Cash note'")
    headers = page.locator('#availableAccountsTableWrapper thead th').all_text_contents()
    assert headers[:5] == ['Code', 'Name', 'Classification', 'FS Category', 'Note']
    assert page.locator('#availableAccountsTableBody tr:first-child td').nth(2).inner_text() == 'Leaf'
    assert page.locator('#availableAccountsTableBody tr:first-child td').nth(3).inner_text() == 'Cash and cash equivalents'
    assert page.locator('#availableAccountsTableBody tr:first-child td').nth(4).inner_text() == 'Cash note'
    assert page.locator('#availableAccountsTableBody tr:nth-child(2) td').nth(3).inner_text() == 'Unassigned'
    assert page.locator('#availableAccountsTableBody tr:nth-child(2) td').nth(4).inner_text() == 'Unassigned'
    assert page.locator('#availableAccountsTableBody tr:first-child td:nth-child(4) input, #availableAccountsTableBody tr:first-child td:nth-child(4) select').count() == 0
    assert page.locator('#tbSavedCategorySearch').is_enabled()
    options = page.locator('#tbSavedCategoryOptions option').evaluate_all('items => items.map(item => item.value)')
    assert options == ['Cash and cash equivalents', 'Trade receivables', 'Unassigned']
    assert page.locator('#tbSavedNoteSearch').is_enabled()
    note_options = page.locator('#tbSavedNoteOptions option').evaluate_all('items => items.map(item => item.value)')
    assert note_options == ['Cash note', 'Receivables note', 'Unassigned']
    category_filter = page.locator('#tbSavedCategorySearch')
    category_filter.fill('receiv')
    assert page.locator('#availableAccountsTableBody tr[data-category-filter]:visible').count() == 1
    assert page.locator('#availableAccountsTableBody tr[data-category-filter]:visible td').nth(1).inner_text() == 'Trade receivables'
    assert page.locator('#availableAccountsTableBody tr.tb-draft-heading td.tb-amount-cell').all_text_contents() == ['30.00', '2.00', '3.00', '1.00', '32.00', '4.00']
    page.locator('#tbSavedNoteSearch').fill('cash')
    assert page.locator('#availableAccountsTableBody tr[data-category-filter]:visible').count() == 0
    assert page.locator('#tbSavedCategoryNoMatches').is_visible()
    assert page.locator('#availableAccountsTableBody tr.tb-draft-heading td.tb-amount-cell').all_text_contents() == ['0.00'] * 6
    category_filter.fill('')
    assert page.locator('#availableAccountsTableBody tr[data-category-filter]:visible td').nth(1).inner_text() == 'Cash'
    assert page.locator('#availableAccountsTableBody tr.tb-draft-heading td.tb-amount-cell').all_text_contents() == ['10.00', '0.00', '5.00', '1.00', '12.00', '0.00']
    page.locator('#tbSavedNoteSearch').fill('unassigned')
    assert page.locator('#availableAccountsTableBody tr[data-category-filter]:visible td').nth(1).inner_text() == 'Unassigned account'
    assert page.locator('#availableAccountsTableBody tr.tb-draft-heading td.tb-amount-cell').all_text_contents() == ['20.00', '2.00', '4.00', '1.00', '23.00', '3.00']
    page.locator('#tbSavedNoteSearch').fill('')
    category_filter.fill('Unassigned')
    assert page.locator('#availableAccountsTableBody tr[data-category-filter]:visible').count() == 1
    category_filter.fill('not a category')
    assert page.locator('#tbSavedCategoryNoMatches').is_visible()
    category_filter.fill('')
    assert page.locator('#availableAccountsTableBody tr[data-category-filter]:visible').count() == 3
    assert page.locator('#availableAccountsTableBody tr.tb-draft-heading td').count() == 1
    assert page.locator('#availableAccountsTableBody tr.tb-draft-heading td').first.inner_text() == 'Draft Trial Balance reconciliation'
    assert page.locator('#availableAccountsTableBody tr:first-child td.tb-amount-cell').first.inner_text() == '10'
    toolbar_rows = page.locator('.tb-saved-toolbar-row')
    assert toolbar_rows.count() == 2
    row_positions = toolbar_rows.evaluate_all('rows => rows.map(row => Math.round(row.getBoundingClientRect().top))')
    assert row_positions[1] > row_positions[0]
    filter_item_tops = toolbar_rows.nth(1).evaluate("row => Array.from(row.children).filter(item => item.getClientRects().length).map(item => Math.round(item.getBoundingClientRect().top))")
    assert max(filter_item_tops) - min(filter_item_tops) <= 2
    page.close()


def test_saved_trial_balance_remains_available_when_category_lookup_fails(browser):
    page = browser.new_page()
    page.set_content('<div id="availableAccountsTableWrapper"><table><thead><tr><th>Code</th></tr></thead><tbody id="availableAccountsTableBody"></tbody></table></div><div id="tbSavedStickyHead"></div><select id="availableUploadsDropdown"><option value="59">TB</option></select><input id="tbSavedCategorySearch"><datalist id="tbSavedCategoryOptions"></datalist><p id="tbSavedCategoryNoMatches" hidden></p>')
    page.add_script_tag(path=str(PROJECT_ROOT / 'static' / 'account_assignment_shared.js'))
    page.add_script_tag(path=str(PROJECT_ROOT / 'static' / 'financial_statement_workspace.js'))
    page.add_script_tag(path=str(PROJECT_ROOT / "static" / "cashflow_workspace.js"))
    page.add_script_tag(path=str(PROJECT_ROOT / 'static' / 'trial_balance.js'))
    page.evaluate("""() => {
        window.API_BASE=''; window.getSelectedClientId=()=>42;
        window.fetch=async url => url.includes('/api/trial-balance/accounts')
            ? {ok:true,json:async()=>({accounts:[{id:1,account_code:'A',account_name:'Cash',is_leaf:true,closing_dr:'50'}],adjustments:[],validation_controls:{}})}
            : {ok:false,json:async()=>({detail:'ACC unavailable'})};
        availableUploadsDropdown.value='59';
        return loadAvailableUploadAccounts('59');
    }""")
    page.wait_for_function("document.querySelector('#tbSavedCategoryNoMatches')?.textContent.includes('categories and notes are unavailable')")
    assert page.locator('#availableAccountsTableWrapper tbody tr').count() == 1
    assert page.locator('#availableAccountsTableWrapper tbody tr:first-child td').nth(3).inner_text() == 'Unavailable'
    assert page.locator('#availableAccountsTableWrapper tbody tr:first-child td.tb-amount-cell').nth(4).inner_text() == '50'
    assert page.locator('#tbSavedCategorySearch').is_disabled()
    assert 'categories and notes are unavailable' in page.locator('#tbSavedCategoryNoMatches').inner_text()
    page.close()


def test_saved_trial_balance_keeps_category_filter_when_note_lookup_fails(browser):
    import re
    html = (PROJECT_ROOT / 'static' / 'trial_balance.html').read_text(encoding='utf-8')
    page = browser.new_page()
    page.set_content(re.sub(r'<script[\s\S]*?</script>', '', html))
    page.add_script_tag(path=str(PROJECT_ROOT / 'static' / 'account_assignment_shared.js'))
    page.add_script_tag(path=str(PROJECT_ROOT / 'static' / 'financial_statement_workspace.js'))
    page.add_script_tag(path=str(PROJECT_ROOT / "static" / "cashflow_workspace.js"))
    page.add_script_tag(path=str(PROJECT_ROOT / 'static' / 'trial_balance.js'))
    page.evaluate("""() => {
        window.API_BASE=''; window.getSelectedClientId=()=>42;
        window.fetch=async url => {
            if (url.includes('/api/trial-balance/accounts')) return {ok:true,json:async()=>({accounts:[
                {id:1,account_code:'A',account_name:'Cash',is_leaf:true,closing_dr:'50'}],adjustments:[],validation_controls:{}})};
            if (url.includes('/api/account-control-center/mapping')) return {ok:true,json:async()=>({client_id:42,upload:{id:59},accounts:[
                {account_code:'A',assignment:{financial_statement_category_id:7,note_category_id:11}}]})};
            if (url.includes('category_type=note')) return {ok:false,json:async()=>({detail:'Notes temporarily unavailable'})};
            return {ok:true,json:async()=>({categories:[{id:7,label:'Cash and cash equivalents',is_active:true}]})};
        };
        availableTrialBalancesSection.style.display='block';
        availableUploadsDropdown.innerHTML='<option value="59">TB</option>';
        availableUploadsDropdown.value='59';
        return loadAvailableUploadAccounts('59');
    }""")
    page.wait_for_function("document.querySelector('#availableAccountsTableBody tr:first-child td:nth-child(4)')?.textContent === 'Cash and cash equivalents' && document.querySelector('#availableAccountsTableBody tr:first-child td:nth-child(5)')?.textContent === 'Unavailable'")
    assert page.locator('#tbSavedCategorySearch').is_enabled()
    assert page.locator('#tbSavedNoteSearch').is_disabled()
    assert page.locator('#availableAccountsTableBody tr:first-child td.tb-amount-cell').nth(4).inner_text() == '50'
    page.locator('#tbSavedCategorySearch').fill('cash and cash')
    assert page.locator('#availableAccountsTableBody tr[data-category-filter]:visible').count() == 1
    assert 'notes are unavailable' in page.locator('#tbSavedCategoryNoMatches').inner_text()
    page.close()


def test_manual_total_row_mapping_preview_gating_and_file_reset(browser):
    import re
    html = (PROJECT_ROOT / 'static' / 'trial_balance.html').read_text(encoding='utf-8')
    page = browser.new_page(viewport={'width': 1280, 'height': 1000})
    page.set_content(re.sub(r'<script[\s\S]*?</script>', '', html))
    page.add_script_tag(path=str(PROJECT_ROOT / 'static' / 'account_assignment_shared.js'))
    page.add_script_tag(path=str(PROJECT_ROOT / 'static' / 'financial_statement_workspace.js'))
    page.add_script_tag(path=str(PROJECT_ROOT / "static" / "cashflow_workspace.js"))
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
    page.add_script_tag(path=str(PROJECT_ROOT / 'static' / 'account_assignment_shared.js'))
    page.add_script_tag(path=str(PROJECT_ROOT / 'static' / 'financial_statement_workspace.js'))
    page.add_script_tag(path=str(PROJECT_ROOT / "static" / "cashflow_workspace.js"))
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
    page.add_script_tag(path=str(PROJECT_ROOT / 'static' / 'account_assignment_shared.js'))
    page.add_script_tag(path=str(PROJECT_ROOT / 'static' / 'financial_statement_workspace.js'))
    page.add_script_tag(path=str(PROJECT_ROOT / "static" / "cashflow_workspace.js"))
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
    page.add_script_tag(path=str(PROJECT_ROOT / 'static' / 'account_assignment_shared.js'))
    page.add_script_tag(path=str(PROJECT_ROOT / 'static' / 'financial_statement_workspace.js'))
    page.add_script_tag(path=str(PROJECT_ROOT / "static" / "cashflow_workspace.js"))
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
    page.add_script_tag(path=str(PROJECT_ROOT / 'static' / 'account_assignment_shared.js'))
    page.add_script_tag(path=str(PROJECT_ROOT / 'static' / 'financial_statement_workspace.js'))
    page.add_script_tag(path=str(PROJECT_ROOT / "static" / "cashflow_workspace.js"))
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
        const saved=Array.from(document.querySelectorAll('#availableAccountsTableBody .tb-draft-adjusted_totals td')).slice(5,11).map(c=>c.textContent);
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
    page.add_script_tag(path=str(PROJECT_ROOT / "static" / "cashflow_workspace.js"))
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


def test_assignment_workflows_preserve_filters_amounts_and_reject_late_results(browser):
    import re
    html = (PROJECT_ROOT / 'static' / 'trial_balance.html').read_text(encoding='utf-8')
    page = browser.new_page(viewport={'width': 1280, 'height': 800})
    page.set_content(re.sub(r'<script[\s\S]*?</script>', '', html))
    page.add_script_tag(path=str(PROJECT_ROOT / 'static' / 'account_assignment_shared.js'))
    page.add_script_tag(path=str(PROJECT_ROOT / 'static' / 'financial_statement_workspace.js'))
    page.add_script_tag(path=str(PROJECT_ROOT / "static" / "cashflow_workspace.js"))
    page.add_script_tag(path=str(PROJECT_ROOT / 'static' / 'trial_balance.js'))
    page.evaluate("""async () => {
        window.API_BASE=''; window.getSelectedClientId=()=>1; window.calls=[]; window.cashflowSetupReads=0;
        const accounts=[{id:1,account_code:'A',account_name:'Cash account',is_leaf:true,opening_dr:'10.25',closing_dr:'20.5'},
                        {id:2,account_code:'B',account_name:'Bank account',is_leaf:true,opening_dr:'30',closing_dr:'40'}];
        const mapping={client_id:1,upload:{id:77},accounts:[
            {account_code:'A',assignment:{financial_statement_category_id:7,note_category_id:11,verification_workstream:'cash'}},
            {account_code:'B',assignment:{financial_statement_category_id:8,note_category_id:null,verification_workstream:'bank'}}]};
        window.fetch=async (url, options={})=>{
            window.calls.push({url,method:options.method||'GET'});
            if(url.includes('/trial-balance/accounts')) return {ok:true,json:async()=>({accounts,adjustments:[],validation_controls:{draft_reconciliation:null}})};
            if(url.includes('/mapping?')) return {ok:true,json:async()=>mapping};
            if(url.includes('/cashflow/setup')) {
                const response={ok:true,json:async()=>({client_id:1,upload:{id:77},accounts:[{account_code:'A',category_id:1},{account_code:'B',category_id:2}],categories:[{id:1,label:'Receipts',is_active:true},{id:2,label:'Payments',is_active:true}],historical_orphans:[],treasury_accounts:{cash:[],bank:[]},settings:{},role_categories:{}})};
                if(++window.cashflowSetupReads <= 2) return new Promise(resolve=>{window.releaseCashflow=()=>resolve(response);});
                return response;
            }
            return {ok:true,json:async()=>({categories:url.includes('category_type=note')?[{id:11,label:'Cash note',is_active:true}]:[{id:7,label:'Cash',is_active:true},{id:8,label:'Bank',is_active:true}]})};
        };
        availableTrialBalancesSection.style.display='block';
        availableUploadsDropdown.innerHTML='<option value="77">TB</option>';
        await loadAvailableUploadAccounts('77');
    }""")
    page.wait_for_function("document.querySelector('.tb-saved-note-cell').textContent === 'Cash note'")
    before = page.locator('tr[data-category-filter]').evaluate_all('rows=>rows.map(row=>row.dataset.tbAmounts)')
    page.locator('#tbSavedCategorySearch').fill('Cash')
    page.locator('#tbSavedNoteSearch').fill('Cash note')
    assert page.locator('.tb-draft-heading .tb-amount-cell').all_text_contents() == ['10.25','0.00','0.00','0.00','20.50','0.00']
    page.evaluate('availableAccountsTableWrapper.scrollLeft=130')
    page.locator('#tbAssignmentWorkflow').select_option('cashflow')
    page.wait_for_function('typeof window.releaseCashflow === "function"')
    # A delayed Cashflow response must not paint over the later Verification selection.
    page.locator('#tbAssignmentWorkflow').select_option('verification')
    page.wait_for_function("document.querySelector('.tb-saved-category-cell').textContent === 'Cash vouchers'")
    page.evaluate('window.releaseCashflow()')
    page.wait_for_function("document.querySelector('#tbSavedCategoryLabel').textContent === 'Verification Workstream'")
    assert page.locator('#tbSavedNoteSearch').is_hidden()
    assert page.locator('#availableAccountsTableWrapper thead th:visible').count() == 0  # synchronized heading is the visible copy
    assert page.locator('#tbSavedStickyHead th:not([hidden])').count() == 10
    assert page.locator('#tbSavedStickyHead col').count() == 10
    page.locator('#tbSavedCategorySearch').fill('bank')
    assert page.locator('tr[data-category-filter]:visible').count() == 1
    assert page.locator('tr[data-category-filter]:visible').get_attribute('data-account-code') == 'B'
    page.locator('#tbAssignmentWorkflow').select_option('cashflow')
    page.wait_for_function("tbWorkflowMode === 'cashflow' && typeof window.releaseCashflow === 'function'")
    page.evaluate('window.releaseCashflow()')
    page.wait_for_function("document.querySelector('.tb-saved-category-cell').textContent === 'Receipts'")
    page.locator('#tbSavedCategorySearch').fill('Receipts')
    assert page.locator('tr[data-category-filter]:visible').count() == 1
    page.evaluate('tbFsIsAdmin = true')
    page.locator('#tbAssignCategoriesBtn').click()
    page.wait_for_selector('#cfwBackdrop:not([hidden])')
    assert page.locator('#cfwBackdrop').get_by_role('tab', name='Categories').count() == 1
    assert page.url == 'about:blank'
    page.locator('#cfwBackdrop [data-cfw-close]').click()
    page.locator('#tbAssignmentWorkflow').select_option('financial')
    assert page.locator('#tbSavedCategorySearch').input_value() == 'Cash'
    assert page.locator('#tbSavedNoteSearch').input_value() == 'Cash note'
    assert page.locator('#tbSavedStickyHead col').count() == 11
    page.wait_for_function('availableAccountsTableWrapper.scrollLeft > 0')
    assert page.locator('tr[data-category-filter]').evaluate_all('rows=>rows.map(row=>row.dataset.tbAmounts)') == before
    assert page.evaluate('window.calls.every(call=>call.method === "GET")')
    assert page.evaluate('window.calls.filter(call=>call.url.includes("/trial-balance/accounts")).length') == 1
    page.locator('#tbAssignmentSetup').click()
    assert page.locator('#tbAssignmentSetupMenu').is_hidden()
    assert page.locator('#tbAssignmentSetupMenu a').count() == 0
    page.keyboard.press('Escape')
    assert page.locator('#tbAssignmentSetupMenu').is_hidden()
    page.evaluate("""() => {
        tbWorkflowCache.delete('cashflow');
        const original=window.fetch;
        window.fetch=(url,options)=>url.includes('/cashflow/setup')
            ? Promise.resolve({ok:true,json:async()=>({client_id:2,upload:{id:999},accounts:[],categories:[]})})
            : original(url,options);
    }""")
    page.locator('#tbAssignmentWorkflow').select_option('cashflow')
    page.wait_for_function("document.querySelector('#tbSavedCategoryNoMatches').textContent.includes('assignments are unavailable')")
    assert page.locator('#tbSavedCategorySearch').is_disabled()
    assert page.locator('tr[data-category-filter]').evaluate_all('rows=>rows.map(row=>row.dataset.tbAmounts)') == before
    page.close()


def test_legacy_cashflow_setup_bookmark_opens_the_matching_trial_balance_panel(browser):
    import re
    html = (PROJECT_ROOT / 'static' / 'trial_balance.html').read_text(encoding='utf-8')
    page = browser.new_page()
    page.set_content(re.sub(r'<script[\s\S]*?</script>', '', html))
    for script in ('account_assignment_shared.js', 'financial_statement_workspace.js', 'cashflow_workspace.js', 'trial_balance.js'):
        page.add_script_tag(path=str(PROJECT_ROOT / 'static' / script))
    page.evaluate("""async () => {
        window.API_BASE=''; window.getSelectedClientId=()=>1; tbFsIsAdmin=true;
        availableUploadsDropdown.innerHTML='<option value="77">TB</option>';
        window.setupCalls=[];
        window.fetch=async url=>{setupCalls.push(url);return {ok:true,json:async()=>({client_id:1,upload:{id:77},accounts:[],categories:[],historical_orphans:[],treasury_accounts:{cash:[],bank:[]},settings:{},role_categories:{}})}};
        await tbOpenLegacySetup('template');
    }""")
    assert page.locator('#cfwBackdrop').is_visible()
    assert page.locator('#cfwBackdrop [data-cfw-panel="template"]').is_visible()
    setup_calls = page.evaluate('setupCalls')
    assert setup_calls and 'trial_balance_upload_id=77' in setup_calls[0], setup_calls
    page.close()


def test_verification_workspace_edits_accounts_and_opens_direct_assignment_popup(browser):
    import re

    html = (PROJECT_ROOT / 'static' / 'trial_balance.html').read_text(encoding='utf-8')
    page = browser.new_page(viewport={'width': 1440, 'height': 900})
    page.set_content(re.sub(r'<script[\s\S]*?</script>', '', html))
    for script in ('loading_ui.js', 'account_assignment_shared.js', 'financial_statement_workspace.js', 'cashflow_workspace.js', 'verification_workspace.js', 'trial_balance.js'):
        page.add_script_tag(path=str(PROJECT_ROOT / 'static' / script))
    page.evaluate("""async () => {
        window.API_BASE=''; window.getSelectedClientId=()=>1; tbFsIsAdmin=true;
        window.calls=[]; window.confirm=()=>true;
        const rows=[
            {id:1,account_code:'10',account_name:'Receivables',is_leaf:false,opening_dr:'100',closing_dr:'125'},
            {id:2,account_code:'10-1',parent_account_code:'10',account_name:'Customer A',is_leaf:true,opening_dr:'60',closing_dr:'70'},
            {id:3,account_code:'10-2',parent_account_code:'10',account_name:'Customer B',is_leaf:true,opening_dr:'40',closing_dr:'55'},
            {id:4,account_code:'99',account_name:'Unlinked parent',is_leaf:false,opening_dr:'0',closing_dr:'0'}
        ];
        window.mapping={client_id:1,upload:{id:77},accounts:rows.map(row=>({account_code:row.account_code,account_name:row.account_name,parent_account_code:row.parent_account_code||null,is_leaf:row.is_leaf,level:row.is_leaf?2:1,assignment:{financial_statement_category_id:7,note_category_id:11,verification_workstream:row.account_code==='10-2'?'bank':null}}))};
        window.fetch=async (url,options={})=>{
            const method=options.method||'GET'; window.calls.push({url,method,body:options.body||null});
            if(url.includes('/trial-balance/accounts')) return {ok:true,json:async()=>({accounts:rows,adjustments:[],validation_controls:{draft_reconciliation:null}})};
            if(url.includes('/mapping/apply-to-children')) {
                const body=JSON.parse(options.body); window.mapping.accounts.filter(item=>item.account_code.startsWith(body.parent_account_code+'-')&&item.is_leaf).forEach(item=>item.assignment.verification_workstream=body.verification_workstream);
                return {ok:true,json:async()=>({saved:true,leaf_count:2})};
            }
            if(url.includes('/mapping') && method==='PUT') {
                const body=JSON.parse(options.body); const target=window.mapping.accounts.find(item=>item.account_code===body.account_code); target.assignment.verification_workstream=body.verification_workstream;
                return {ok:true,json:async()=>({saved:true})};
            }
            if(url.includes('/mapping?')) return {ok:true,json:async()=>window.mapping};
            return {ok:true,json:async()=>({})};
        };
        window.ApiUtils={apiFetch:(url,options)=>window.fetch(url,options),apiFetchMutate:(url,options)=>window.fetch(url,options)};
        availableTrialBalancesSection.style.display='block';
        availableUploadsDropdown.innerHTML='<option value="77">Test upload</option>';
        tbWorkflowMode='verification'; tbAssignmentWorkflow.value='verification';
        await loadAvailableUploadAccounts('77');
    }""")

    page.wait_for_function("document.querySelector('.tb-verification-workstream-picker')")
    assert page.locator('#tbSavedCategoryLabel').text_content() == 'Verification Workstream'
    assert page.locator('tr[data-account-code="10-1"] .tb-saved-category-cell select').input_value() == ''
    assert page.locator('tr[data-account-code="10-2"] .tb-saved-category-cell select').input_value() == 'bank'
    assert page.locator('tr[data-account-code="99"] .tb-verification-workstream-picker').is_disabled()
    before = page.locator('tr[data-category-filter]').evaluate_all('rows=>rows.map(row=>row.dataset.tbAmounts)')
    mapping_reads = page.evaluate("calls.filter(call=>call.url.includes('/mapping?')).length")
    page.locator('tr[data-account-code="10-1"] .tb-verification-workstream-picker').select_option('payroll')
    page.wait_for_function("mapping.accounts.find(item=>item.account_code==='10-1').assignment.verification_workstream==='payroll'")
    page.wait_for_function("document.querySelector('tr[data-account-code=\\\"10-1\\\"] .tb-verification-workstream-picker').value==='payroll'")
    assert page.evaluate("calls.filter(call=>call.url.includes('/mapping?')).length") == mapping_reads

    page.locator('#tbAssignmentSetup').click()
    assert page.locator('#vwBackdrop').is_visible()
    assert page.locator('#vwRows tr').count() == 4
    page.locator('#vwSearch').fill('Customer A')
    assert page.locator('#vwRows tr').count() == 1
    page.locator('#vwSearch').fill('')
    page.locator('#vwRows tr.vw-unlinked').get_by_text('Check code').wait_for()
    page.locator('#vwRows select[data-verification-code="10"]').select_option('cash')
    page.wait_for_function("calls.some(call=>call.url.includes('/mapping/apply-to-children')) && mapping.accounts.find(item=>item.account_code==='10-1').assignment.verification_workstream==='cash'")
    assert page.locator('tr[data-account-code="10-1"] .tb-verification-workstream-picker').input_value() == 'cash'
    assert page.locator('tr[data-account-code="10-2"] .tb-verification-workstream-picker').input_value() == 'cash'
    assert page.locator('tr[data-account-code="10-1"]').get_attribute('data-tb-amounts') is not None
    assert page.locator('tr[data-category-filter]').evaluate_all('rows=>rows.map(row=>row.dataset.tbAmounts)') == before
    writes = page.evaluate("calls.filter(call=>call.method==='PUT'||call.method==='POST')")
    assert [write['method'] for write in writes] == ['PUT', 'POST']
    assert writes[0]['url'].endswith('/mapping')
    assert writes[1]['url'].endswith('/mapping/apply-to-children')
    assert page.evaluate("JSON.parse(calls.find(call=>call.method==='PUT').body).verification_workstream") == 'payroll'
    assert page.evaluate("JSON.parse(calls.find(call=>call.method==='POST').body).verification_workstream") == 'cash'
    page.locator('#vwBackdrop [data-vw-close]').click()
    assert page.locator('#vwBackdrop').is_hidden()
    page.close()


def test_financial_parent_can_reapply_same_category_to_all_descendant_leaves(browser):
    import re

    html = (PROJECT_ROOT / 'static' / 'trial_balance.html').read_text(encoding='utf-8')
    page = browser.new_page()
    page.set_content(re.sub(r'<script[\s\S]*?</script>', '', html))
    for script in ('loading_ui.js', 'account_assignment_shared.js', 'financial_statement_workspace.js', 'cashflow_workspace.js', 'verification_workspace.js', 'trial_balance.js'):
        page.add_script_tag(path=str(PROJECT_ROOT / 'static' / script))
    page.evaluate("""async () => {
        window.API_BASE=''; window.getSelectedClientId=()=>1; tbFsIsAdmin=true;
        window.confirm=()=>true; window.calls=[];
        const rows=[
            {id:1,account_code:'10-10-25',account_name:'Fixed assets',parent_account_code:'10-10',is_leaf:false,closing_dr:'100'},
            {id:2,account_code:'10-10-25-10',account_name:'Property plant and equipment',parent_account_code:'10-10-25',is_leaf:false,closing_dr:'100'},
            {id:3,account_code:'10-10-25-10-0001',account_name:'Factory land',parent_account_code:'10-10-25-10',is_leaf:true,closing_dr:'60'},
            {id:4,account_code:'10-10-25-10-0002',account_name:'Factory building',parent_account_code:'10-10-25-10',is_leaf:true,closing_dr:'40'},
            {id:5,account_code:'10-10-30',account_name:'Other fixed assets',parent_account_code:'10-10',is_leaf:false,closing_dr:'20'},
            {id:6,account_code:'10-10-30-0001',account_name:'Other asset A',parent_account_code:'10-10-30',is_leaf:true,closing_dr:'10'},
            {id:7,account_code:'10-10-30-0002',account_name:'Other asset B',parent_account_code:'10-10-30',is_leaf:true,closing_dr:'10'},
            {id:8,account_code:'10-10-35',account_name:'Other asset group',parent_account_code:'10-10',is_leaf:false,closing_dr:'10'},
            {id:9,account_code:'10-10-35-0001',account_name:'Other asset C',parent_account_code:'10-10-35',is_leaf:true,closing_dr:'10'}
        ];
        window.mapping={client_id:1,upload:{id:77},accounts:rows.map(row=>({account_code:row.account_code,account_name:row.account_name,parent_account_code:row.parent_account_code,is_leaf:row.is_leaf,level:row.account_code.split('-').length,assignment:{financial_statement_category_id:row.is_leaf?(row.account_code.startsWith('10-10-25-')?8:(row.account_code.endsWith('0001')?4:null)):4,note_category_id:row.is_leaf?(row.account_code.startsWith('10-10-25-')?21:null):22}}))};
        window.fetch=async (url,options={})=>{
            const method=options.method||'GET'; window.calls.push({url,method,body:options.body||null});
            if(url.includes('/api/trial-balance/accounts')) return {ok:true,json:async()=>({accounts:rows,adjustments:[],validation_controls:{}})};
            if(url.includes('/mapping/apply-to-children')) {
                const body=JSON.parse(options.body);
                const leaves=window.mapping.accounts.filter(item=>item.account_code.startsWith(body.parent_account_code+'-')&&item.is_leaf);
                leaves.forEach(item=>{item.assignment.financial_statement_category_id=body.financial_statement_category_id;});
                return {ok:true,json:async()=>({saved:true,client_id:1,trial_balance_upload_id:77,assignment_changes:leaves.map(item=>({account_code:item.account_code,financial_statement_category_id:8,note_category_id:21}))})};
            }
            if(url.includes('/mapping?')) return {ok:true,json:async()=>window.mapping};
            if(url.includes('category_type=note')) return {ok:true,json:async()=>({categories:[
                {id:20,label:'Other assets note',source_statement_category_id:8,parent_category_id:null,is_active:true},
                {id:21,label:'Asset details',source_statement_category_id:null,parent_category_id:20,is_active:true},
                {id:22,label:'Old fixed assets note',source_statement_category_id:4,parent_category_id:null,is_active:true}
            ]})};
            if(url.includes('/categories?')) return {ok:true,json:async()=>({categories:[
                {id:3,label:'Non Current Assets',statement_section:'balance_sheet',is_active:true,row_type:'heading'},
                {id:4,label:'Fixed assets',statement_section:'balance_sheet',parent_category_id:3,is_active:true,row_type:'category'},
                {id:8,label:'Other non current assets',statement_section:'balance_sheet',parent_category_id:3,is_active:true,row_type:'category'}
            ]})};
            return {ok:true,json:async()=>({})};
        };
        window.ApiUtils={apiFetch:(url,options)=>window.fetch(url,options),apiFetchMutate:(url,options)=>window.fetch(url,options)};
        availableTrialBalancesSection.style.display='block';
        availableUploadsDropdown.innerHTML='<option value="77">Test upload</option>';
        availableUploadsDropdown.value='77'; tbWorkflowMode='financial'; tbAssignmentWorkflow.value='financial';
        await loadAvailableUploadAccounts('77');
    }""")

    page.wait_for_function("document.querySelector('tr[data-account-code=\\\"10-10-25\\\"] [data-fs-picker]:not(.acc2-note-picker) [data-fs-picker-trigger]')")
    parent_picker = page.locator('tr[data-account-code="10-10-25"] [data-fs-picker]:not(.acc2-note-picker) [data-fs-picker-trigger]')
    assert parent_picker.locator('[data-fs-picker-value-label]').inner_text() == 'Other non current assets'
    assert parent_picker.get_attribute('title') == 'Balance Sheet › Non Current Assets › Other non current assets'
    assert '10-10-25: Balance Sheet › Non Current Assets › Other non current assets' in parent_picker.get_attribute('aria-label')
    nested_picker = page.locator('tr[data-account-code="10-10-25-10"] [data-fs-picker]:not(.acc2-note-picker) [data-fs-picker-trigger]')
    assert nested_picker.locator('[data-fs-picker-value-label]').inner_text() == 'Other non current assets'
    parent_note_picker = page.locator('tr[data-account-code="10-10-25"] .acc2-note-picker [data-fs-picker-trigger]')
    assert parent_note_picker.locator('[data-fs-picker-value-label]').inner_text() == 'Asset details'
    assert parent_note_picker.get_attribute('title') == 'Other non current assets › Asset details'
    assert page.locator('tr[data-account-code="10-10-25-10"] .acc2-note-picker [data-fs-picker-value-label]').inner_text() == 'Asset details'
    mixed_picker = page.locator('tr[data-account-code="10-10-30"] [data-fs-picker]:not(.acc2-note-picker) [data-fs-picker-trigger]')
    assert mixed_picker.locator('[data-fs-picker-value-label]').inner_text() == 'Mixed (2)'
    assert 'Balance Sheet › Non Current Assets › Fixed assets (1 account)' in mixed_picker.get_attribute('title')
    assert 'Unassigned (1 account)' in mixed_picker.get_attribute('title')
    unassigned_note = page.locator('tr[data-account-code="10-10-30-0001"] .tb-saved-note-cell .acc2-fs-picker-trigger')
    unassigned_category = page.locator('tr[data-account-code="10-10-30-0002"] .tb-saved-category-cell .acc2-fs-picker-trigger')
    assert 'is-unassigned' in unassigned_note.get_attribute('class').split()
    assert 'is-unassigned' in unassigned_category.get_attribute('class').split()
    assert unassigned_note.locator('[data-fs-picker-value-label]').evaluate("el=>getComputedStyle(el).color") == 'rgb(180, 35, 24)'
    assert unassigned_note.locator('[data-fs-picker-value-label]').evaluate("el=>getComputedStyle(el).fontWeight") == '700'
    assert 'is-unassigned' not in mixed_picker.get_attribute('class').split()
    next_button = page.locator('#tbNextUnassigned')
    assert next_button.is_visible() and next_button.is_enabled()
    assert page.locator('#tbNextUnassignedCount').inner_text() == '3'
    assert next_button.get_attribute('aria-label') == 'Next unassigned category or note (3)'
    next_box = next_button.bounding_box()
    setup_box = page.locator('#tbAssignmentSetup').bounding_box()
    assert next_box['y'] + next_box['height'] < setup_box['y']
    next_button.click()
    page.wait_for_function("document.querySelector('tr[data-account-code=\\\"10-10-30-0001\\\"] .tb-saved-note-cell .acc2-fs-picker.is-open')", timeout=5000)
    open_picker = page.locator('#availableAccountsTableBody [data-fs-picker].is-open')
    assert open_picker.count() == 1
    assert not page.evaluate("calls.some(call=>call.method==='POST')")
    assert open_picker.locator('select').get_attribute('data-acc-code') == '10-10-30-0001'
    assert open_picker.locator('select').get_attribute('class') == 'acc2-note-category'
    page.keyboard.press('Escape')
    next_button.click()
    page.wait_for_function("document.querySelector('tr[data-account-code=\\\"10-10-30-0002\\\"] .tb-saved-category-cell .acc2-fs-picker.is-open')")
    page.keyboard.press('Escape')
    next_button.click()
    page.wait_for_function("document.querySelector('tr[data-account-code=\\\"10-10-35\\\"] .tb-saved-note-cell .acc2-fs-picker.is-open')")
    page.keyboard.press('Escape')
    next_button.click()
    page.wait_for_function("document.querySelector('tr[data-account-code=\\\"10-10-30-0001\\\"] .tb-saved-note-cell .acc2-fs-picker.is-open')")
    page.keyboard.press('Escape')
    page.locator('#tbSavedCategoryTrigger').click()
    page.locator('#tbSavedCategorySearch').fill('Other non current assets')
    page.wait_for_function("document.querySelector('#tbNextUnassigned').disabled && document.querySelector('#tbNextUnassigned').title==='No unassigned in this view'")
    page.locator('#tbSavedCategorySearch').fill('')
    page.evaluate("() => { tbFsAccounts.filter(account=>account.is_leaf).forEach(account=>{ if(account.assignment.financial_statement_category_id==null){account.assignment.financial_statement_category_id=8;account.assignment.note_category_id=21;} else if(String(account.assignment.financial_statement_category_id)==='4' && account.assignment.note_category_id==null){account.assignment.note_category_id=22;} }); tbRenderFinancialAssignments(); }")
    page.wait_for_function("document.querySelector('#tbNextUnassigned').disabled && document.querySelector('#tbNextUnassigned').title==='All assigned'")
    parent_note_picker.click()
    assert page.locator('tr[data-account-code="10-10-25"] .acc2-note-picker [data-fs-picker-option]').filter(has_text='Other non current assets › Asset details').count() == 1
    parent_note_picker.click()
    parent_picker.click()
    page.locator('tr[data-account-code="10-10-25"] [data-fs-picker]:not(.acc2-note-picker) [data-fs-picker-option][data-fs-picker-value="8"]').click()

    page.wait_for_function("calls.some(call=>call.url.includes('/mapping/apply-to-children')) && mapping.accounts.filter(item=>item.account_code.startsWith('10-10-25-')&&item.is_leaf).every(item=>item.assignment.financial_statement_category_id===8)")
    writes = page.evaluate("calls.filter(call=>call.method==='POST')")
    assert len(writes) == 1
    assert writes[0]['url'].endswith('/mapping/apply-to-children')
    assert page.evaluate("JSON.parse(calls.find(call=>call.method==='POST').body).parent_account_code") == '10-10-25'
    page.close()


def _batch_code_page(browser):
    import re
    page = browser.new_page()
    html = (PROJECT_ROOT / 'static/trial_balance.html').read_text(encoding='utf-8')
    page.set_content(re.sub(r'<script[\s\S]*?</script>', '', html))
    for script in ('account_assignment_shared.js', 'financial_statement_workspace.js', 'trial_balance.js'):
        page.add_script_tag(path=str(PROJECT_ROOT / 'static' / script))
    page.evaluate("""() => {
        window.API_BASE=''; window.getSelectedClientId=()=>5;
        availableTrialBalancesSection.style.display='block'; availableAccountsTableWrapper.style.display='block';
        availableUploadsDropdown.innerHTML='<option value="59">Half-year</option><option value="60">Other</option>';
        availableCategoryUploadId='59'; tbWorkflowMode='financial';
        window.rows=[{id:1,account_code:'BAD1',account_name:'Parent 1',is_leaf:false,closing_dr:'42'},
            {id:2,account_code:'BAD2',account_name:'Parent 2',is_leaf:false,closing_dr:'17'}];
        renderAvailableAccountsTable(window.rows, []);
        window.calls=[];window.reloads=0;window.failSave=false;
        window.fetch=async(url,options)=>{
            const body=JSON.parse(options.body);window.calls.push({url,body});
            if(window.failSave)return {ok:false,json:async()=>({detail:'Conflicting mapping'})};
            body.corrections.forEach(c=>window.rows.find(row=>row.id===c.account_id).account_code=c.account_code);
            return {ok:true,json:async()=>({updated:true,upload_id:59,corrected_count:body.corrections.length})};
        };
        loadAvailableUploadAccounts=async()=>{window.reloads++;renderAvailableAccountsTable(window.rows,[]);return true;};
    }""")
    return page


def _edit_saved_code(page, row, value):
    cell=page.locator('#availableAccountsTableBody tr').nth(row).locator('td').first
    cell.locator('.tb-account-code-display').dblclick()
    cell.locator('input').fill(value)
    cell.locator('input').press('Enter')


def test_saved_code_batch_waits_for_save_and_refreshes_once(browser):
    page=_batch_code_page(browser)
    _edit_saved_code(page,0,'10')
    _edit_saved_code(page,1,'20')
    assert page.evaluate('calls.length') == 0
    assert page.locator('#tbCodeCorrectionsCount').inner_text() == '2 unsaved code correction(s)'
    assert page.locator('#availableAccountsTableBody tr').first.get_attribute('data-account-code') == 'BAD1'
    page.locator('#tbSaveCodeCorrections').click()
    page.wait_for_function('reloads===1 && !tbCodeSaveRunning')
    assert page.evaluate('calls.length') == 1
    assert page.evaluate('calls[0].body.corrections') == [
        {'account_id':1,'account_code':'10','expected_account_code':'BAD1'},
        {'account_id':2,'account_code':'20','expected_account_code':'BAD2'}]
    assert page.locator('#tbCodeCorrectionsBar').is_hidden()
    assert page.evaluate('rows.map(row=>row.closing_dr)') == ['42','17']
    page.close()


def test_saved_code_draft_guard_cancel_discard_and_failed_save(browser):
    page=_batch_code_page(browser)
    _edit_saved_code(page,0,'10')
    # Stay preserves the draft and prevents the requested workflow action.
    page.locator('#tbAssignmentSetup').evaluate('el=>el.disabled=false')
    page.locator('#tbAssignmentSetup').click()
    assert page.locator('#tbCodeCorrectionsDialog').is_visible()
    page.locator('#tbCodeStay').click()
    assert page.evaluate('tbPendingCodeCorrections.size') == 1
    page.evaluate('failSave=true')
    page.locator('#tbSaveCodeCorrections').click()
    page.wait_for_function('!tbCodeSaveRunning && calls.length===1')
    assert page.evaluate('tbPendingCodeCorrections.size') == 1
    assert 'Conflicting mapping' in page.locator('#tbSavedStatus').inner_text()
    assert page.evaluate('reloads') == 0
    page.locator('#tbDiscardCodeCorrections').click()
    assert page.locator('.tb-account-code-display').first.inner_text() == 'BAD1'
    assert page.evaluate('tbPendingCodeCorrections.size') == 0
    page.close()


def test_saved_code_guard_saves_before_toolbar_action_and_keyboard_upload_change(browser):
    page=_batch_code_page(browser)
    _edit_saved_code(page,0,'10')
    page.locator('#closeAvailableTrialBalancesBtn').click()
    page.locator('#tbCodeSave').click()
    page.wait_for_function('calls.length===1 && !tbCodeSaveRunning')
    assert page.locator('#availableTrialBalancesSection').is_hidden()
    page.close()
    page=_batch_code_page(browser)
    _edit_saved_code(page,1,'20')
    page.evaluate("availableUploadsDropdown.value='60';availableUploadsDropdown.dispatchEvent(new Event('change',{bubbles:true}))")
    assert page.locator('#tbCodeCorrectionsDialog').is_visible()
    assert page.locator('#availableUploadsDropdown').input_value() == '59'
    page.locator('#tbCodeStay').click()
    assert page.evaluate('tbPendingCodeCorrections.size') == 1
    page.close()


def test_import_code_edits_stage_without_requests_and_require_one_revalidation(browser):
    page=_batch_code_page(browser)
    page.evaluate("""() => {
        tbDiscardAccountCodes();availableTrialBalancesSection.style.display='none';tbValidationSheet.hidden=false;
        tbValidationSheet.classList.add('active');fileHeaders=['Code','Name'];
        tbColumnMapping={account_code_column:'Code',account_name_column:'Name'};
        fileAllRows=[['BAD1','Parent 1'],['BAD2','Parent 2']];
        renderTrialBalanceValidationGrid();validationDirty=false;
    }""")
    for row,code in [(0,'10'),(1,'20')]:
        cell=page.locator('#tbValidationBody tr').nth(row).locator('.tb-account-code-display')
        cell.dblclick()
        editor=page.locator('#tbValidationBody input')
        editor.fill(code);editor.press('Enter')
    assert page.evaluate('accountCodeCorrections') == {'2':'10','3':'20'}
    assert page.evaluate('validationDirty') is True
    assert page.evaluate('calls.length') == 0
    assert page.locator('#uploadTrialBalanceBtn').is_disabled()
    page.close()


def test_import_unlinked_parent_confirmation_can_cancel_before_upload(browser):
    page=_batch_code_page(browser)
    page.evaluate("""() => {
        selectedFile=new File(['data'],'tb.xlsx');window.getSelectedClientId=()=>5;
        periodStartInput.value='2026-01-01';periodEndInput.value='2026-06-30';
        validationResult={valid:true,warnings:[{message:'Parent has no linked child accounts.'}]};validationDirty=false;
        checkMappingComplete=()=>true;checkTotalRowChoice=()=>true;
        window.confirmations=[];window.confirm=message=>{confirmations.push(message);return false;};
    }""")
    page.evaluate('uploadTrialBalance()')
    assert page.evaluate('confirmations.length') == 1
    assert page.evaluate('calls.length') == 0
    page.close()
