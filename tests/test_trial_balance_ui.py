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
        fileAllRows = [['A1', 'Cash', '100', '0', '20', '0', '110', '0']];
        validationResult = {rollforward_differences: [{
            excel_row: 2, account_code: 'A1', account_name: 'Cash',
            closing_dr_adjustment: 10, closing_cr_adjustment: 0
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
            savedDebit: document.querySelector('#availableAccountsTableBody tr.tb-generated-difference td:nth-child(7)')?.textContent
        };
    }""")

    assert result["originalClosingDebit"] == "110"
    assert result["differenceLabel"] == "Difference — Cash"
    assert result["differenceClosingDebit"] == "10"
    assert "System-generated differences (1)" in result["validationSection"]
    assert "System-generated differences (1)" in result["savedSection"]
    assert result["savedLabel"] == "Difference — Cash"
    assert result["savedDebit"] == "10"

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
    assert cells[2:8] == ['', '100', '', '', '', '100']
    assert cells[8] == 'Unassigned global difference'
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
    assert result['matched']['leaf'] == ['100.00', '100.00', '20.00', '20.00', '110.00', '110.00']
    assert result['matched']['erp'] == result['matched']['leaf']
    assert result['matched']['difference'] == ['0.00'] * 6
    assert result['matched']['label'] == 'Client ERP total'
    assert 'tb-control-difference' in result['matched']['lastRow']
    assert result['matched']['banner'] == ''
    assert result['matched']['zeroCells'] == 6
    assert result['matched']['footerEditors'] == 0
    assert result['matched']['exact'] == '9,007,199,254,740,993.01'
    assert result['nonzero'] == {'value': '0.00', 'red': 0}
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
                    .map(formatTrialBalanceControlAmount),
            cells:Array.from(document.querySelectorAll('.tb-control-difference .tb-validation-amount'))
                .map(cell=>({text:cell.textContent,red:cell.classList.contains('tb-footing-nonzero'),title:cell.title})),
            unchanged:JSON.stringify(validationResult)===original};
    }""")
    assert result['amounts']==['1,234,567,890.20','9,007,199,254,740,993.02','-1.01','999.50','0.00']
    assert [cell['text'] for cell in result['cells']]==['0.00','0.00','0.00','0.00','0.02','0.01']
    assert [cell['red'] for cell in result['cells']]==[False,False,False,False,True,True]
    assert '0.0100000000000000000001' in result['cells'][-1]['title']
    assert result['unchanged']
    page.close()


@pytest.mark.parametrize('width',[1920,1280,900])
def test_control_totals_fit_and_scrolling_keeps_headers_aligned(browser,width):
    import re
    html=(PROJECT_ROOT / 'static' / 'trial_balance.html').read_text(encoding='utf-8')
    page=browser.new_page(viewport={'width':width,'height':1080})
    page.set_content(re.sub(r'<script[\s\S]*?</script>','',html))
    page.add_style_tag(path=str(PROJECT_ROOT / 'static' / 'style.css'))
    page.add_script_tag(path=str(PROJECT_ROOT / 'static' / 'trial_balance.js'))
    page.evaluate("""() => {
        fileHeaders=['Code','Name','OD','OC','PD','PC','CD','CC'];
        tbColumnMapping=Object.fromEntries(TB_MAPPING_FIELDS.map((field,index)=>[field.role,fileHeaders[index]]));
        fileAllRows=[['A','Debit',1,0,0,0,1,0],['B','Credit',0,1,0,0,0,1],['','Total',1,1,0,0,1,1]];
        autoClassificationByRow={'2':'leaf','3':'leaf'};
        const roles=['opening_dr_column','opening_cr_column','period_dr_column','period_cr_column','closing_dr_column','closing_cr_column'];
        validationResult={source_total_rows:[4],source_leaf_totals:Object.fromEntries(roles.map(role=>[role,'9007199254740993.015'])),
            checks:[{key:'account_codes',status:'pass'}],source_total_comparison:
            ['Opening Dr','Opening Cr','Period Dr','Period Cr','Closing Dr','Closing Cr']
            .map(field=>({field,source_total:'9007199254740993.015',difference:'-2.97E-7',within_tolerance:true}))};
        tbValidationSheet.hidden=false;tbValidationSheet.classList.add('active');renderTrialBalanceValidationGrid();
        tbValidationTableWrap.scrollLeft=250;
    }""")
    page.wait_for_function("document.querySelector('#tbValidationStickyHead table').style.transform.includes('-250px')")
    measurements=page.evaluate("""() => {
        const cells=Array.from(document.querySelectorAll('.tb-control-leaf td'));
        const headers=Array.from(document.querySelectorAll('#tbValidationStickyHead th'));
        return cells.map((cell,i)=>{
            const range=document.createRange();range.selectNodeContents(cell);
            const style=getComputedStyle(cell);
            return {amount:cell.classList.contains('tb-validation-amount'),
                fits:range.getBoundingClientRect().width<=cell.clientWidth-parseFloat(style.paddingLeft)-parseFloat(style.paddingRight)+1,
                aligned:Math.abs(cell.getBoundingClientRect().left-headers[i].getBoundingClientRect().left)<1,
                right:style.textAlign==='right'};
        });
    }""")
    assert all(cell['aligned'] for cell in measurements)
    assert all(cell['fits'] and cell['right'] for cell in measurements if cell['amount'])
    page.close()
