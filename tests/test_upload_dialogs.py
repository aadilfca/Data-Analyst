"""Offline Chromium checks of the actual Upload assets; no API or database."""
from pathlib import Path
import re

import pytest
from playwright.sync_api import sync_playwright, expect


ROOT = Path(__file__).resolve().parents[1]


def test_upload_uses_template_comparisons_without_editable_controls_or_saved_overrides(page):
    page.route('**/api/upload/saved-mapping*',lambda route:route.fulfill(json={
        'has_saved_mapping':True,'mappings':[{'template_column_id':1,'excel_column':'Voucher','compare_for_changes':False}]}))
    page.evaluate('''async()=>{
        selectedTemplateId=1; excelHeaders=['Voucher'];
        window.getSelectedClientId=()=>5;
        currentMappings=[{column_id:1,column_name:'Voucher',data_type:'Text',excel_column:'Voucher',is_matched:true,compare_for_changes:true}];
        renderMapping(currentMappings);
        await applySavedMapping();
    }''')
    assert page.locator('#mappingBody input[type=checkbox]').count()==0
    assert 'Compare for changes' not in page.locator('#mappingPanel').inner_text()
    assert page.evaluate('currentMappings[0].compare_for_changes') is True


def test_development_backup_policy_control_and_review_warning(page):
    page.route('**/api/upload/backup-policy', lambda route: route.fulfill(
        json={'allow_without_backup': True}))
    page.evaluate('loadUploadBackupPolicy()')
    assert page.locator('#uploadBackupEnabled').count() == 0
    assert page.evaluate('uploadBackupRequired') is False
    expect(page.locator('#uploadBackupHelp')).to_contain_text('No recovery backup')
    review(page)
    page.locator('#uploadReplacementCancel').click()
    page.evaluate('void reviewUploadReplacement({...window.reviewPayload, backup_enabled:false})')
    expect(page.locator('#uploadReplacementBackup')).to_contain_text('no recovery ZIP or saved Excel report')


def test_backup_review_shows_real_drive_folder_link_when_configured(page):
    page.evaluate("uploadArchiveFolderUrl='https://drive.google.com/drive/folders/archive123'")
    page.evaluate('uploadDriveConnectionReady=true')
    review(page)
    link = page.locator('#uploadReplacementBackup a')
    expect(link).to_have_attribute('href', 'https://drive.google.com/drive/folders/archive123')
    expect(page.locator('#uploadReplacementBackup')).to_contain_text('Upload Change Reports / client / year / import date')


def test_review_exposes_known_folder_but_warns_if_drive_connection_is_incomplete(page):
    page.evaluate("uploadArchiveFolderUrl='https://drive.google.com/drive/folders/archive123'; uploadDriveConnectionReady=false")
    review(page)
    expect(page.locator('#uploadReplacementBackup a')).to_have_attribute(
        'href', 'https://drive.google.com/drive/folders/archive123')
    expect(page.locator('#uploadReplacementBackup')).to_contain_text('Drive connection is incomplete')


def test_known_oauth_failure_is_not_shown_as_an_unconfirmed_import(page):
    page.route('**/api/upload/import', lambda route: route.fulfill(status=503, json={'detail': {
        'message': 'Google Drive authorization expired.', 'code': 'drive_auth_reconnect_required',
        'outcome': 'not_applied'}}))
    state = page.evaluate('''async () => {
        try { await fetchUpload('/api/upload/import', {method:'POST'}); }
        catch (error) {
            showUploadFailure(error.message, 'Import', error);
            return {outcomeUnknown:error.outcomeUnknown};
        }
    }''')
    assert state['outcomeUnknown'] is False
    expect(page.locator('#statusMessage')).to_contain_text('Import not applied')
    expect(page.locator('.upload-issue-action')).to_contain_text('Reconnect this app to Google Drive')


def test_success_popup_shows_saved_report_and_folder_links(page):
    page.evaluate('''() => showImportSummary({status:'success', total_rows:1, imported_count:1,
        change_report:{file_url:'https://drive.google.com/file/d/report/view',
          folder_url:'https://drive.google.com/drive/folders/day',
          folder_path:'Upload Change Reports/Leather-Crop/2026/2026-11-03'}})''')
    expect(page.locator('#importSummarySavedReport')).to_contain_text('Verified Excel change report saved')
    expect(page.locator('#importSummarySavedReport a').nth(0)).to_have_attribute(
        'href', 'https://drive.google.com/file/d/report/view')
    expect(page.locator('#importSummarySavedReport')).to_contain_text('Leather-Crop/2026/2026-11-03')


def test_success_popup_reports_actual_added_changed_deleted_and_unchanged_rows(page):
    page.evaluate('''() => showImportSummary({status:'success', total_rows:4, imported_count:1,
        updated_count:1, deleted_count:1, change_detection:{comparison_mode:'replace_range', unchanged_count:1,
          new_count:1, changed_count:1, missing_count:1}})''')
    summary = page.locator('#importSummaryOverlay').inner_text()
    assert '1 new row added · 1 existing row changed · 1 old row deleted' in summary
    assert 'Unchanged — kept in database' in summary
    assert 'Incoming new — added' in summary
    assert 'Changed — updated' in summary
    assert 'Deleted — removed' in summary
    assert 'Unchanged rows stay in the database and are not imported again.' in summary


def test_ignored_import_explains_that_unchanged_rows_were_not_imported(page):
    page.evaluate('''() => showImportSummary({status:'success', ignored:true, total_rows:99,
        imported_count:0, updated_count:0, deleted_count:0,
        change_detection:{unchanged_count:99, new_count:0, changed_count:0, missing_count:0}})''')
    summary = page.locator('#importSummaryOverlay').inner_text()
    assert 'All validated transactions were already in the database.' in summary
    assert '99' in summary and 'Unchanged — kept in database' in summary
    assert '0 new row' not in summary


def test_unavailable_backup_policy_keeps_backup_required(page):
    page.route('**/api/upload/backup-policy', lambda route: route.fulfill(status=503, body='unavailable'))
    page.evaluate('loadUploadBackupPolicy()')
    assert page.evaluate('uploadBackupRequired') is True
    expect(page.locator('#uploadBackupHelp')).to_contain_text('Could not check')


def test_administrator_revocation_restores_backup_and_invalidates_waiting_import(page):
    policy = {'allow_without_backup': True}
    page.route('**/api/upload/backup-policy', lambda route: route.fulfill(json=policy))
    page.evaluate('loadUploadBackupPolicy()')
    assert page.evaluate('uploadBackupRequired') is False
    page.evaluate("validationPlanId='ready';validationStatus='valid'")
    policy['allow_without_backup'] = False
    page.evaluate('loadUploadBackupPolicy()')
    assert page.evaluate('uploadBackupRequired') is True
    expect(page.locator('#uploadBackupHelp')).to_contain_text('Backups are ON')
    assert page.evaluate('validationPlanId') is None


@pytest.fixture
def settings_page():
    html = (ROOT/'static/settings.html').read_text(encoding='utf-8')
    html = re.sub(r'<script\b[^>]*>.*?</script>', '', html, flags=re.S|re.I)
    html = re.sub(r'<link\b[^>]*>', '', html, flags=re.I)
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        current = browser.new_page(viewport={'width': 768, 'height': 900})
        current.route('**/*', lambda route: route.fulfill(status=200, content_type='text/html', body=html)
                      if route.request.resource_type == 'document' else route.abort())
        current.goto('http://settings-tests.invalid/')
        current.add_script_tag(content='var API_BASE="";\n' + (ROOT/'static/settings.js').read_text(encoding='utf-8'))
        # This fixture loads scripts after the load event to avoid starting the
        # unrelated legacy Settings requests. Wire the actual save action.
        current.evaluate("document.getElementById('saveUploadBackupsBtn').addEventListener('click',saveUploadBackupSetting)")
        yield current
        browser.close()


def test_visible_settings_switch_saves_and_reloads_without_restart(settings_page):
    page = settings_page
    state = {'allow_without_backup': False}
    def endpoint(route):
        if route.request.method == 'POST':
            state.update(route.request.post_data_json)
        route.fulfill(json=state)
    page.route('**/api/settings/upload-backups', endpoint)
    page.evaluate('loadUploadBackupSetting()')
    toggle = page.locator('#uploadBackupRequired')
    expect(page.locator('#uploadBackupsPanel')).to_be_visible()
    expect(toggle).to_be_checked()
    toggle.uncheck()
    page.locator('#saveUploadBackupsBtn').click()
    expect(page.locator('#uploadBackupsStatus')).to_contain_text('Saved.')
    page.evaluate('loadUploadBackupSetting()')
    expect(toggle).not_to_be_checked()
    toggle.check()
    page.locator('#saveUploadBackupsBtn').click()
    expect(page.locator('#uploadBackupsStatus')).to_contain_text('Backups are ON for all users.')
    assert state['allow_without_backup'] is False


def test_settings_hides_administrator_switch_for_nonadmin_and_reports_failed_save(settings_page):
    page = settings_page
    page.route('**/api/settings/upload-backups', lambda route: route.fulfill(status=403, body='Forbidden'))
    page.evaluate('loadUploadBackupSetting()')
    expect(page.locator('#uploadBackupsPanel')).to_be_hidden()
    page.unroute('**/api/settings/upload-backups')
    page.route('**/api/settings/upload-backups', lambda route:
               route.fulfill(json={'allow_without_backup': False}) if route.request.method == 'GET'
               else route.fulfill(status=503, body='Unavailable'))
    page.evaluate('loadUploadBackupSetting()')
    page.locator('#uploadBackupRequired').uncheck()
    page.locator('#saveUploadBackupsBtn').click()
    expect(page.locator('#uploadBackupsStatus')).to_contain_text('Save was not confirmed.')
    page.evaluate('loadUploadBackupSetting()')
    expect(page.locator('#uploadBackupRequired')).to_be_checked()


def test_validation_excel_report_is_generated_in_browser(page):
    page.add_script_tag(path=str(ROOT/'static/vendor/xlsx.full.min.js'))
    page.evaluate('''() => {
        window.reportFile=null;
        XLSX.writeFile=(book,name)=>window.reportFile={name,
            workbook:XLSX.write(book,{type:'base64',bookType:'xlsx'})};
        downloadChangeReport([{action:'CHANGED',old_values:{Name:'Old'},new_values:{Name:'New'}}],{upload_id:12});
    }''')
    result = page.evaluate('window.reportFile')
    assert result['name'] == 'upload-change-report-12.xlsx'
    import base64, io
    from openpyxl import load_workbook
    book = load_workbook(io.BytesIO(base64.b64decode(result['workbook'])))
    assert book.active.cell(2,15).value == 'CHANGED'
    assert book.active.cell(2,20).value == '{"Name":"Old"}'


@pytest.fixture
def page():
    html = (ROOT/'static/upload.html').read_text(encoding='utf-8')
    html = re.sub(r'<script\b[^>]*>.*?</script>', '', html, flags=re.S|re.I)
    html = re.sub(r'<link\b[^>]*>', '', html, flags=re.I)
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        current = browser.new_page(viewport={'width':1366,'height':900})
        current.route('**/*', lambda route: route.fulfill(status=200, content_type='text/html', body=html)
                      if route.request.resource_type=='document' else route.abort())
        current.goto('http://upload-dialog-tests.invalid/')
        current.add_script_tag(content='var API_BASE="/api"; function getSelectedClientId(){return 1;} function getSelectedClientName(){return "Leather Crop";}\n'+
                               (ROOT/'static/upload.js').read_text(encoding='utf-8'))
        yield current
        browser.close()


def review(page, summary=None, moves=None):
    values = summary if summary is not None else dict(unchanged_count=2250,new_count=7470,
        changed_count=0,missing_count=2436,changed_voucher_count=842,removed_voucher_count=1)
    if moves is not None:
        values = {**values,'date_moves':moves}
    page.evaluate('''summary => {
        templateDropdown.replaceChildren(new Option('Transaction Report','1',true,true));
        lastChangePreview=summary;
        window.summaryBefore=JSON.stringify(summary);
        window.reviewPayload={coverage_start:'2026-10-01',coverage_end:'2026-11-02'};
        window.reviewDecision=null;
        reviewUploadReplacement(window.reviewPayload).then(value=>window.reviewDecision=value);
    }''', values)
    expect(page.locator('#uploadReplacementReview')).to_be_visible()


def date_errors(page, count=51):
    page.evaluate('''count => showValidationErrors({error_count:count,rejected_count:0,total_rows:count,
        errors:Array.from({length:count},(_,i)=>({row_number:i+2,row_data:{'Voucher ID':'BPV-1026-101'},
          errors:[{column:'Transaction Date',excel_column:'Date',error_type:'data_type',message:'Enter a valid date in the selected format.'}]}))})''',count)


def test_review_tables_preserve_all_counts_scope_and_safeguards(page):
    page.evaluate("uploadArchiveFolderUrl='https://drive.google.com/drive/folders/archive123'")
    review(page)
    rows = page.locator('#uploadReplacementCounts tr').all_text_contents()
    assert rows == [
        'Unchanged — already in database — not imported2,250',
        'Incoming new — will be added7,470',
        'Changed — existing rows to update0',
        'Rows with unchecked-field values to refresh0',
        'Deleted — absent from this complete report2,436',
    ]
    expect(page.locator('#uploadReplacementChangedVouchers')).to_have_text('842')
    expect(page.locator('#uploadReplacementRemovedVouchers')).to_have_text('1')
    expect(page.locator('#uploadReplacementScope')).to_have_text('Leather Crop · Transaction Report')
    expect(page.locator('#uploadReplacementPeriod')).to_have_text('01 Oct 2026 – 02 Nov 2026')
    assert 'verified in Google Drive before import.' in page.locator('.upload-review-notes').inner_text()
    expect(page.locator('#uploadReplacementBackup a')).to_have_attribute(
        'href', 'https://drive.google.com/drive/folders/archive123')
    assert 'same voucher may be replaced outside this period' in page.locator('.upload-review-notes').inner_text()
    assert 'Outside this period: kept.' in page.locator('.upload-review-notes').inner_text()
    assert page.evaluate('JSON.stringify(lastChangePreview)===window.summaryBefore')


def test_review_back_import_and_escape_resolve_existing_confirmation(page):
    for action, expected in [('back',False),('import',True),('escape',False),('import',True)]:
        review(page)
        if action=='escape':
            page.keyboard.press('Escape')
        else:
            page.locator('#uploadReplacementCancel' if action=='back' else '#uploadReplacementConfirm').click()
        page.wait_for_function('window.reviewDecision!==null')
        assert page.evaluate('window.reviewDecision') is expected
        expect(page.locator('#uploadReplacementReview')).not_to_be_visible()


def test_review_date_moves_are_tabular_safe_and_cleared_on_reopen(page):
    review(page,moves=[dict(voucher_id='<img src=x onerror=alert(1)>',previous_dates=['2026-09-30'],incoming_dates=['2026-10-01'])])
    expect(page.locator('#uploadReplacementDateMoves')).to_be_visible()
    assert page.locator('#uploadReplacementDateMoveRows tr').inner_text()=='<img src=x onerror=alert(1)>\t2026-09-30\t2026-10-01'
    assert page.locator('#uploadReplacementDateMoves img').count()==0
    page.keyboard.press('Escape')
    review(page,summary={})
    expect(page.locator('#uploadReplacementDateMoves')).not_to_be_visible()
    expect(page.locator('#uploadReplacementVouchers')).not_to_be_visible()
    assert page.locator('#uploadReplacementDateMoveRows tr').count()==0
    assert page.locator('#uploadReplacementCounts .upload-review-count').all_text_contents()==['0']*5


def test_validation_summary_is_short_and_only_affected_field_is_red(page):
    date_errors(page,9720)
    expect(page.locator('#uploadIssueTitle')).to_have_text('Fix upload errors')
    expect(page.locator('.upload-issue-summary')).to_have_text('9,720 rows need correction')
    expect(page.locator('.upload-issue-action')).to_have_text('Check Date format, then validate again.')
    expect(page.locator('.upload-issue-field').first).to_have_text('Transaction Date')
    assert page.locator('.upload-issue-field').first.evaluate('(el)=>getComputedStyle(el).color')=='rgb(189, 37, 44)'
    assert page.locator('.upload-issue-fix').first.evaluate('(el)=>getComputedStyle(el).color')!='rgb(189, 37, 44)'
    assert page.locator('.upload-issue-details tbody tr').count()==50
    assert '0 rejected' not in page.get_by_role('dialog').inner_text()


def test_error_paging_escape_and_focus_remain_available(page):
    date_errors(page)
    page.get_by_role('button',name='Next',exact=True).click()
    assert page.locator('.upload-issue-details tbody tr').count()==1
    expect(page.locator('.upload-issue-navigation span')).to_have_text('Showing 51–51 of 51')
    expect(page.get_by_role('button',name='Next',exact=True)).to_be_disabled()
    page.keyboard.press('Tab')
    assert page.evaluate("!!document.activeElement.closest('#validationErrorOverlay')")
    page.keyboard.press('Escape')
    assert page.get_by_role('dialog').count()==0


def test_row_links_still_open_correct_source_page(page):
    page.evaluate('''() => {
        document.getElementById('leftPanel').style.display='block';
        rowExclusionHeaders=['Voucher ID','Date'];
        rowExclusionAllRows=Array.from({length:60},(_,i)=>['V'+i,'01/10/2026']);
        currentMappings=[{excel_column:'Voucher ID'}];
    }''')
    date_errors(page)
    page.locator('.upload-issue-row-link').filter(has_text=re.compile('^25$')).click()
    assert page.locator('#validationErrorOverlay').count()==0
    assert page.evaluate('rowExclusionCurrentPage')==3
    assert page.evaluate('document.activeElement.closest("tr").dataset.excelRowNumber')=='25'


def test_multiple_fields_and_rejections_share_one_source_row_without_html_injection(page):
    page.evaluate('''() => showValidationErrors({error_count:1,rejected_count:1,total_rows:1,
      errors:[{row_number:2,row_data:{Voucher:'<img src=x>'},errors:[{column:'<script>bad</script>',message:'Enter a number.'}]}],
      rejected_rows:[{row_number:2,reasons:[{column:'Transaction Date',message:'Missing Date.'}]}]})''')
    assert page.locator('.upload-issue-details tbody tr').count()==1
    assert page.locator('.upload-issue-field').all_text_contents()==['<script>bad</script>','Transaction Date']
    expect(page.locator('.upload-issue-summary')).to_have_text('1 row needs correction · 1 rejected')
    assert page.locator('#validationErrorOverlay img, #validationErrorOverlay script').count()==0


def test_source_warnings_keep_their_details_without_error_red(page):
    page.evaluate("showUploadIssue({stage:'Source review',severity:'warning',message:'Review the source total.',rows:[{row_number:52,column:'Debit Amount',message:'Source total differs.'}]})")
    expect(page.locator('#uploadIssueTitle')).to_have_text('Upload warning')
    expect(page.locator('.upload-issue-label')).to_have_text('Debit Amount')
    assert page.locator('.upload-issue-field').count()==0
    assert 'Source total differs.' in page.get_by_role('dialog').inner_text()


@pytest.mark.parametrize('width',[360,768])
def test_dialogs_fit_narrow_screens(page,width):
    page.set_viewport_size({'width':width,'height':900})
    review(page)
    assert page.locator('#uploadReplacementReview').evaluate('(el)=>el.scrollWidth<=el.clientWidth')
    page.keyboard.press('Escape')
    date_errors(page,6)
    assert page.locator('#validationErrorOverlay section').evaluate('(el)=>el.scrollWidth<=el.clientWidth')
    assert page.locator('#validationErrorOverlay').evaluate('(el)=>el.getBoundingClientRect().right<=window.innerWidth')


def prepare_source_controls(page):
    page.evaluate('''() => {
        workbookData = new Uint8Array([1]);
        selectedTemplateId = 1; fileId = 99; currentSheet = 'Vouchers';
        currentMappings = [
            {column_id:1,column_name:'Voucher ID',data_type:'Text',excel_column:'Voucher ID',is_matched:true,reconciliation_role:'document_id'},
            {column_id:2,column_name:'Date',data_type:'Date',excel_column:'Date',is_matched:true},
            {column_id:3,column_name:'Debit',data_type:'Numeric',excel_column:'Debit',is_matched:true,reconciliation_role:'footing'},
            {column_id:4,column_name:'Credit',data_type:'Numeric',excel_column:'Credit',is_matched:true,reconciliation_role:'footing'}
        ];
        rowExclusionHeaders = ['Voucher ID','Date','Debit','Credit'];
        excelHeaders = rowExclusionHeaders.slice();
        rowExclusionAllRows = [['A','15/04/2026',10,0],['A','15/04/2026',10,0],['B','15/04/2026',0,20],['TOTAL','',20,20]];
        excludedRowNumbers = new Set();
        leftPanel.style.display='block'; mappingPanel.style.display='block'; modeActions.style.display='block';
        document.getElementById('rowExclusionContainer').style.display='block';
        renderMapping(currentMappings);
        resetSourceControlChoice(true); renderRowExclusionPage();
    }''')


def test_footer_suggestion_requires_confirmation_and_is_not_a_transaction_checkbox(page):
    prepare_source_controls(page)
    assert page.locator('#sourceControlRow').input_value() == '5'
    assert not page.locator('#sourceControlConfirmed').is_checked()
    assert page.locator('#sourceControlPreview').inner_text() == 'Row 5: Debit: 20 · Credit: 20'
    page.locator('#sourceControlConfirmed').check()
    assert page.locator('[data-excel-row-number="5"] input').is_disabled()
    assert not page.locator('[data-excel-row-number="5"] input').is_checked()
    assert page.locator('#rowExclusionCounter').inner_text() == '3 of 3 rows selected · 1 control total excluded'
    page.locator('#rowExclusionHeadRow input').uncheck()
    assert page.locator('#rowExclusionCounter').inner_text() == '0 of 3 rows selected · 1 control total excluded'
    page.locator('#rowExclusionHeadRow input').check()
    assert page.evaluate('excludedRowNumbers.size') == 0
    page.locator('#sourceControlRow').fill('4')
    assert not page.locator('#sourceControlConfirmed').is_checked()
    assert page.locator('[data-excel-row-number="5"] input').is_enabled()


@pytest.mark.parametrize('change', ['footer', 'row', 'all_rows', 'mapping', 'date', 'override'])
def test_source_control_inputs_and_selection_clear_validation_plan(page, change):
    prepare_source_controls(page)
    page.locator('#sourceControlConfirmed').check()
    page.evaluate("validationStatus='valid'; validationPlanId='approved-plan';")
    if change == 'footer': page.locator('#sourceControlRow').fill('4')
    if change == 'row': page.locator('[data-excel-row-number="2"] input').uncheck()
    if change == 'all_rows': page.locator('#rowExclusionHeadRow input').uncheck()
    if change == 'mapping': page.locator('#mappingBody select').first.select_option('')
    if change == 'date':
        page.locator('#uploadCoverageStart').fill('2026-04-01')
        page.locator('#uploadCoverageStart').dispatch_event('change')
    if change == 'override':
        page.evaluate("setRejectedRows([{row_number:3,reasons:[{column:'Date',message:'Blank date'}]}]);")
        page.locator('#rowRejectionList input').check()
    assert page.evaluate('validationPlanId') is None
    assert page.evaluate('validationStatus') == 'idle'
    if change == 'mapping': assert not page.locator('#sourceControlConfirmed').is_checked()


def test_source_control_results_show_full_and_partial_scope_without_injecting_html(page):
    prepare_source_controls(page)
    page.evaluate('''() => renderSourceControls({status:'matched',control_total_row:5,
        full_sheet:{row_count:3,document_count:2},approved_detail:{row_count:2,document_count:1},
        scope:{complete_period:false},checks:[{column:'Debit <img src=x>',excel_total:'20',detail_total:'20',difference:'0'}]})''')
    text = page.locator('#sourceControlResults').inner_text()
    assert 'Full sheet detail: 3 rows · 2 unique documents' in text
    assert 'Approved for import: 2 rows · 1 unique documents' in text
    assert 'full-period database equality is not established' in text
    assert page.locator('#sourceControlResults img').count() == 0
    page.evaluate('resetSourceControlChoice(true)')
    assert page.locator('#sourceControlResults').inner_text() == ''
    assert not page.locator('#sourceControlConfirmed').is_checked()


@pytest.mark.parametrize('width', [390, 768, 1280])
def test_compact_source_controls_fit_mapping_area(page, width):
    prepare_source_controls(page)
    page.set_viewport_size({'width':width,'height':900})
    assert page.locator('#sourceControlPanel').evaluate('(el) => el.scrollWidth <= el.clientWidth')
    assert page.locator('#sourceControlPanel').is_visible()


def test_no_footing_template_has_no_extra_upload_controls(page):
    prepare_source_controls(page)
    page.evaluate("currentMappings.forEach(m=>m.reconciliation_role=null); resetSourceControlChoice();")
    assert page.locator('#sourceControlPanel').is_hidden()


def test_source_control_validation_hint_clears_and_exact_amount_display_keeps_small_differences(page):
    prepare_source_controls(page)
    page.evaluate("showUploadFieldError(getElement('sourceControlRow'),getElement('sourceControlPreview'),'Confirm a total row');")
    page.locator('#sourceControlRow').fill('4')
    assert page.locator('#sourceControlRow').get_attribute('aria-invalid') is None
    assert page.locator('#sourceControlPreview').is_visible()
    assert page.evaluate("formatSourceControlAmount('77108960.00000001')") == '77,108,960.00000001'
    assert page.evaluate("formatSourceControlAmount('-0.000000000000000001')") == '-0.000000000000000001'


def import_reconciliation(status='matched'):
    return {'status': status, 'message': {'matched':'Approved Excel controls match stored data at completion.',
        'unavailable':'Stored-data verification is unavailable.', 'mismatch':'Reconciliation differences need review.',
        'not_comparable':'Partial extract: full-period reconciliation is not certified.'}[status],
        'scope': {'client_id':1,'client_name':'Leather Crop','template_id':1,'template_name':'Transaction Report',
                  'complete_period':status!='not_comparable','date_range':{'start':'2026-01-01','end':'2026-01-01'}},
        'checked_at':'2026-11-06T15:00:00+05:00','document_column':'Voucher ID','footing_configured':True,
        'rows':[{'label':'Total rows','excel':116,'stored':116,'difference':'0','matches':True},
                {'label':'Unique documents','excel':29,'stored':29,'difference':'0','matches':True},
                {'label':'Footing — Debit Amount','excel':'77108960.01','stored':'77108960.01','difference':'0','matches':True},
                {'label':'Footing — Credit Amount','excel':'77108960.01','stored':'77108960.01','difference':'0','matches':True}]}


def test_import_result_has_one_three_column_reconciliation_table_after_processed_counts(page):
    page.evaluate('data=>showImportSummary(data)', {'status':'success','total_rows':116,'imported_count':116,
        'change_detection':{'new_count':116},'reconciliation':import_reconciliation()})
    table=page.locator('#importReconciliation table')
    assert table.locator('th').all_text_contents() == ['Metric','Excel detail','Stored data']
    assert table.locator('tbody tr').count() == 4
    assert '77,108,960.01' in table.inner_text()
    assert 'Leather Crop · Transaction Report · 2026-01-01 – 2026-01-01' in page.locator('#importReconciliation').inner_text()
    assert page.locator('#importSummaryOverlay h2').inner_text() == 'Import Successful'
    assert page.locator('#uploadReplacementReview #importReconciliation').count() == 0


@pytest.mark.parametrize('status,title', [('unavailable','Import completed — review reconciliation'),
                                       ('mismatch','Import completed — review reconciliation'),
                                       ('not_comparable','Import Successful')])
def test_result_checks_never_disguise_a_committed_import_as_failed_or_partial_as_matched(page,status,title):
    proof=import_reconciliation(status)
    if status=='unavailable':
        for row in proof['rows']: row.update(stored=None,matches=None,difference=None)
    if status=='mismatch': proof['rows'][0].update(stored=117,matches=False,difference='1')
    if status=='not_comparable':
        for row in proof['rows']: row.update(matches=None,difference=None)
    page.evaluate('proof=>showImportSummary({status:"success",total_rows:116,imported_count:116,reconciliation:proof})',proof)
    assert page.locator('#importSummaryOverlay h2').inner_text() == title
    assert page.locator('#importReconciliation [data-status]').first.get_attribute('data-status') == status
    if status=='mismatch':
        assert page.locator('#importReconciliation tr[data-matches="false"]').get_attribute('title') == 'Stored minus Excel: 1'
        assert 'Stored minus Excel: Total rows: 1.' in page.locator('#importReconciliation').inner_text()
    if status=='unavailable': assert 'Unavailable' in page.locator('#importReconciliation table').inner_text()


def test_reconciliation_scope_and_labels_use_text_and_absent_footing_is_explicit(page):
    proof=import_reconciliation()
    proof['scope']['client_name']='<img src=x onerror=alert(1)>'
    proof['footing_configured']=False
    proof['rows']=proof['rows'][:2]
    page.evaluate('proof=>showImportSummary({status:"success",reconciliation:proof})',proof)
    assert page.locator('#importReconciliation img').count()==0
    assert '<img src=x' in page.locator('#importReconciliation').inner_text()
    assert 'Footing not configured' in page.locator('#importReconciliation').inner_text()


@pytest.mark.parametrize('width',[390,768,1280])
def test_reconciliation_result_fits_narrow_screens_without_losing_counts(page,width):
    page.set_viewport_size({'width':width,'height':900})
    page.evaluate('proof=>showImportSummary({status:"success",reconciliation:proof})',import_reconciliation())
    assert page.locator('#importSummaryOverlay > div').evaluate('(el)=>el.scrollWidth<=el.clientWidth')
    assert page.locator('#importReconciliation table').is_visible()


def test_downloaded_upload_report_contains_exact_reconciliation_and_scope(page):
    page.add_script_tag(path=str(ROOT/'static/vendor/xlsx.full.min.js'))
    page.evaluate('''proof=>{
        XLSX.writeFile=(book,name)=>window.reconciliationReport=XLSX.write(book,{type:'base64',bookType:'xlsx'});
        downloadChangeReport([],{upload_id:99},proof);
    }''',import_reconciliation())
    import base64,io
    from openpyxl import load_workbook
    book=load_workbook(io.BytesIO(base64.b64decode(page.evaluate('window.reconciliationReport'))))
    assert book.sheetnames==['Upload review','Reconciliation']
    sheet=book['Reconciliation']
    assert sheet.cell(3,2).value=='Leather Crop'
    assert sheet.cell(11,1).value=='Metric'
    assert sheet.cell(14,2).value==sheet.cell(14,3).value=='77108960.01'


def test_unchanged_validation_opens_verified_result_without_sending_import(page):
    page.add_script_tag(path=str(ROOT/'static/loading_ui.js'))
    prepare_source_controls(page)
    page.locator('#sourceControlConfirmed').check()
    page.evaluate("localStorage.setItem('selectedClientId','1'); uploadCoverageStart.value='2026-04-01'; uploadCoverageEnd.value='2026-04-30';")
    requests=[]
    page.on('request',lambda request:requests.append(request.url))
    result={'status':'validation_complete','has_errors':False,'errors':[],'rejected_rows':[],
            'total_rows':116,'can_import':False,'change_detection':{'unchanged_count':116},
            'reconciliation':import_reconciliation()}
    page.route('**/api/upload/validate',lambda route:route.fulfill(json=result))
    page.locator('#importBtn').click()
    expect(page.locator('#importSummaryOverlay h2')).to_have_text('No changes required')
    assert not any(url.endswith('/api/upload/import') for url in requests)
