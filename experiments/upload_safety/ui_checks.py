"""Offline browser checks of the original upload error UI; no app server."""
import json
import re

import run as h
from playwright.sync_api import sync_playwright


def main():
    h.RESULTS.extend(json.loads((h.HERE/'results.json').read_text(encoding='utf-8')))
    html=(h.ROOT/'static/upload.html').read_text(encoding='utf-8')
    html=re.sub(r'<script\b[^>]*>.*?</script>', '', html, flags=re.S|re.I)
    html=re.sub(r'<link\b[^>]*>', '', html, flags=re.I)
    with sync_playwright() as pw:
        browser=pw.chromium.launch(headless=True)
        page=browser.new_page(viewport={'width':1366,'height':900})
        page.route('**/*',lambda route:route.fulfill(status=200,content_type='text/html',body=html) if route.request.resource_type=='document' else route.abort())
        page.goto('http://upload-experiment.invalid/')
        page.add_script_tag(content='var API_BASE="/api"; function getSelectedClientId(){return 1;} function getSelectedClientName(){return "Experiment";}\n'+(h.ROOT/'static/upload.js').read_text(encoding='utf-8'))
        def errors():
            actual=next(x for x in h.RESULTS if x['name']=='Reject invalid-date')['detail']['response']
            page.evaluate('(result)=>showValidationErrors(result)',actual)
            dialog=page.get_by_role('dialog')
            text=dialog.inner_text()
            assert 'Enter a valid date in the selected format.' in text,text
            assert 'Transaction Date' in text and 'Excel row' in text,text
            assert dialog.locator('.upload-issue-row-link').first.inner_text()=='2'
            page.screenshot(path=str(h.PRIVATE/'error-dialog.png'),full_page=True)
            page.keyboard.press('Escape')
            assert page.get_by_role('dialog').count()==0
            return {'actual_backend_error_rendered':True,'row_column_and_fix_visible':True,'escape_closes':True,'text':text}
        h.check('Browser displays actual row/date error with short fix and Escape dismissal',errors)
        def safe_text():
            page.evaluate('showUploadIssue({stage:"Validation",message:"<img src=x onerror=alert(1)>",rows:Array.from({length:51},(_,i)=>({row_number:i+2,message:"Fix this value",voucher:"V1"}))})')
            dialog=page.get_by_role('dialog')
            assert dialog.locator('img').count()==0
            assert '<img src=x onerror=alert(1)>' in dialog.inner_text()
            assert dialog.locator('tbody tr').count()==50
            dialog.get_by_role('button',name='Next',exact=True).click()
            assert dialog.locator('tbody tr').count()==1
            assert 'Rows 51–51 of 51' in dialog.inner_text()
            page.keyboard.press('Escape')
            return {'untrusted_text_safe':True,'error_paging_50_then_1':True}
        h.check('Browser error text is safe and long lists paginate',safe_text)
        def inline():
            page.evaluate('''() => {
                document.querySelectorAll('[hidden]').forEach(e=>e.hidden=false);
                document.getElementById('leftPanel').style.display='block';
                document.getElementById('mappingPanel').style.display='block';
                document.getElementById('modeActions').style.display='flex';
                ledgerAccountSelect.disabled=false;
                showUploadFieldError(ledgerAccountSelect,document.getElementById('ledgerAccountHelp'),'Choose a Trial Balance posting account.');
            }''')
            assert page.locator('#ledgerAccountSelect').get_attribute('aria-invalid')=='true'
            assert page.locator('#ledgerAccountHelp').inner_text()=='Choose a Trial Balance posting account.'
            assert page.evaluate('document.activeElement.id')=='ledgerAccountSelect'
            page.evaluate("clearUploadFieldError(ledgerAccountSelect,document.getElementById('ledgerAccountHelp'))")
            assert page.locator('#ledgerAccountSelect').get_attribute('aria-invalid') is None
            return {'field_highlighted':True,'short_fix_visible':True,'focus_on_account':True,'error_clears':True}
        h.check('Browser account error highlights and focuses the correct field',inline)
        def jump():
            page.evaluate('''() => {
                rowExclusionHeaders=['Voucher ID','Date'];
                rowExclusionAllRows=Array.from({length:30},(_,i)=>['V'+i,'01/01/2026']);
                currentMappings=[{excel_column:'Voucher ID'}];
                document.getElementById('rowExclusionContainer').style.display='block';
                showUploadIssue({message:'Fix row 25',rows:[{row_number:25,message:'Enter a date.'}]});
            }''')
            page.locator('.upload-issue-row-link').click()
            assert page.get_by_role('dialog').count()==0
            assert page.locator('[data-excel-row-number="25"]').count()==1
            assert page.evaluate('rowExclusionCurrentPage')==3
            assert page.evaluate('document.activeElement.closest("tr").dataset.excelRowNumber')=='25'
            return {'row_25_opens_page_3':True,'source_checkbox_focused':True}
        h.check('Browser error row link jumps to the correct source page',jump)
        browser.close()


if __name__=='__main__':
    main()
