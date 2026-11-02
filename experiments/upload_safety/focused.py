"""Follow-up controls for source settings, ledger arithmetic and failure diagnosis."""
import hashlib
import json
from pathlib import Path
from unittest.mock import patch
import openpyxl
import run as h


def main():
    h.RESULTS.extend(json.loads((h.HERE/'results.json').read_text(encoding='utf-8')))
    docs=[('Purchase Demand','2. purchase demands Test.xlsx','PD #','Date','Quantity',None),
          ('Purchase Order','3. Purchase Orders Test.xlsx','PO #','Date','Quantity','Total Amount'),
          ('Gate Pass','4. Gate Pass Test.xlsx','GP #','Date','Quantity',None),
          ('GRN','5. GRN Test.xlsx','GRN #','Receiving Date','Quantity','Amount'),
          ('Vendor Bill','6. Vendor bills Test.xlsx','Bill #','Date','Qty','Amount'),
          ('Extended Purchase Order','2. PURCHASE ORDER EXTENDED VIEW (JAN-JUNE).xlsx','PO #','PO Date','Qty','Total'),
          ('Extended Vendor Bill','5. SUPPLIERS BILLING EXTENDED VIEW (JAN-AUG).xlsx','Bill #','Date','Qty','Total')]
    for name,file,key,day,qty,amount in docs:
        def recovered(name=name,file=file,key=key,day=day,qty=qty,amount=amount):
            path=h.ROOT/'uploads'/file;_,headers,data=h.rows(path)
            mapping={'Document ID':key,'Transaction Date':day,'Quantity':qty}
            columns=[('Document ID','Text'),('Transaction Date','Date'),('Quantity','Numeric')]
            if amount:mapping['Amount']=amount;columns.append(('Amount','Numeric'))
            tid,cols=h.template('Recovered '+name,columns);cid=h.client('Recovered '+name)
            p=h.stage(path,cid,tid,cols,mapping,'2026-01-01','2026-12-31')
            ix={n:headers.index(s) for n,s in mapping.items()}
            good=[r for r in data if r[ix['Document ID']] and r[ix['Transaction Date']]]
            sample=good[0][ix['Transaction Date']]
            p['date_delimiter']='-' if isinstance(sample,str) and '-' in sample else '/'
            p['excluded_rows']=[i for i,r in enumerate(data,2) if not r[ix['Document ID']] and not r[ix['Transaction Date']]]
            v=h.validate(p);h.apply(p,v);actual=h.stored(cid,tid)
            assert len(actual)==len(good),(len(actual),len(good))
            from collections import Counter
            assert Counter(str(r['row_data']['Document ID']) for r in actual)==Counter(str(r[ix['Document ID']]) for r in good)
            for field in ['Quantity','Amount']:
                if field in mapping:
                    assert abs(sum(h.number(r[ix[field]]) for r in good)-sum(h.number(r['row_data'][field]) for r in actual))<h.Decimal('0.00001')
            repeat=h.stage(path,cid,tid,cols,mapping,'2026-01-01','2026-12-31')
            repeat.update(date_delimiter=p['date_delimiter'],excluded_rows=p['excluded_rows'])
            again=h.validate(repeat);assert again['duplicate_upload_noop'] and not again['can_import']
            return {'rows':len(good),'delimiter':p['date_delimiter'],'manually_excluded_footer_or_blank_rows':p['excluded_rows'],'totals_match':True,'repeat_noop':True}
        h.check('Document '+name+' succeeds with correct format and footer selection',recovered)
    def balance_arithmetic():
        verified=[]
        def signed(value):
            text=str(value).strip().upper().replace(',','')
            sign=-1 if 'CR' in text else 1
            return h.Decimal(text.replace('DR','').replace('CR','').strip())*sign
        for result in h.RESULTS:
            if not result['name'].startswith('Ledger ') or result['status']!='PASS':continue
            d=result['detail'];code=d['evidence']['ledger_account_code']
            evidence=json.loads(d['evidence']['ledger_evidence'])
            upload=h.query("SELECT id,file_name,client_id,template_id FROM uploaded_files WHERE ledger_account_code=%s AND status='Imported' ORDER BY id LIMIT 1",(code,))[0]
            path=h.ROOT/'uploads'/upload['file_name'];_,headers,data=h.rows(path)
            positions={n:headers.index(n) for n in ['Voucher ID','Date','Debit','Credit','Balance']}
            postings=[r for r in data if r[positions['Voucher ID']] and r[positions['Date']]]
            current=None;differences=[]
            for row in postings:
                debit=h.number(row[positions['Debit']]);credit=h.number(row[positions['Credit']])
                balance=signed(row[positions['Balance']])
                if current is None:current=balance-debit+credit;opening=current
                current+=debit-credit
                if abs(current-balance)>h.Decimal('0.00001'):differences.append(str(current-balance))
            assert not differences,differences[:5]
            assert current==h.Decimal(evidence['balance_check']['closing'])
            assert opening==h.Decimal(evidence['balance_check']['opening'])
            verified.append({'account':code,'checked_postings':len(postings),'opening':str(opening),'closing':str(current)})
        assert len(verified)==6,verified
        return {'independently_reconciled_ledgers':verified}
    h.check('Independent decimal running-balance proof for all six ledgers',balance_arithmetic)
    base=h.PRIVATE/'fixtures'/'voucher-base.xlsx';_,headers,data=h.rows(base)
    names=['Voucher ID','Transaction Date','Account Detail','Account Name','Voucher Detail','Debit Amount','Credit Amount']
    kinds=['Text','Date','Text','Text','Text','Numeric','Numeric']
    tid,cols=h.template('Focused voucher with line IDs',list(zip(names,kinds))+[('Line No','Text')])
    def ids(sheet):
        sheet.cell(1,8).value='Line No'
        for i in range(2,sheet.max_row+1):sheet.cell(i,8).value=str(i-1)
    source=h.variant(base,'explicit-base',ids)
    mapping=dict(zip(names,headers));mapping['Line No']='Line No'
    def payload(path,cid):return h.stage(path,cid,tid,cols,mapping,'2026-01-01','2026-01-31')
    def rollback():
        cid=h.client('explicit rollback');p=payload(source,cid);h.apply(p,h.validate(p));before=h.fingerprint(cid,tid)
        changed=h.variant(source,'explicit-correction',lambda s:setattr(s.cell(2,5),'value','Controlled description correction'))
        cp=payload(changed,cid);v=h.validate(cp)
        with patch.object(h.upload,'upsert_classification_rows_postgres',side_effect=RuntimeError('Injected failure after SQL updates')):
            try:h.apply(cp,v)
            except RuntimeError:pass
            else:raise AssertionError('Post-write failure was not reached')
        assert h.fingerprint(cid,tid)==before,'Failed import changed rows'
        result=h.apply(cp,v)
        assert result['updated_count']==1 and result['deleted_count']==0 and result['archive_verified'],result
        before=h.fingerprint(cid,tid);retry=h.apply(cp,v)
        assert h.fingerprint(cid,tid)==before
        return {'update_in_place':True,'verified_archive':True,'rollback_proven':True,'same_plan_retry':True,'receipt_retry_noop':True}
    h.check('Explicit line-ID correction, real SQL rollback, retry and receipt safety',rollback)
    def archive_failure():
        cid=h.client('explicit archive');p=payload(source,cid);h.apply(p,h.validate(p));before=h.fingerprint(cid,tid)
        changed=h.PRIVATE/'fixtures'/'explicit-correction.xlsx';cp=payload(changed,cid);v=h.validate(cp)
        from services import upload_lifecycle
        with patch.object(upload_lifecycle,'archive_directory',side_effect=RuntimeError('Injected archive failure')):
            try:h.apply(cp,v)
            except RuntimeError:pass
            else:raise AssertionError('Archive failure did not block replacement')
        assert h.fingerprint(cid,tid)==before
        result=h.apply(cp,v);assert result['archive_verified'] and result['updated_count']==1
        return {'failure_preserved_rows':True,'same_plan_retry':True,'archive_verified':True}
    h.check('Archive failure and retry with identifiable correction',archive_failure)
    def nonfinite():
        cid=h.client('nonfinite diagnosis');path=h.PRIVATE/'fixtures'/'nonfinite-debit.xlsx'
        # Seven-column source has no explicit Line No; use its original template.
        t,c=h.template('Nonfinite diagnosis',list(zip(names,kinds)))
        p=h.stage(path,cid,t,c,dict(zip(names,headers)),'2026-01-01','2026-01-31')
        response=h.http.post('/api/upload/validate',json=p)
        error=None
        try:h.upload.validate_only(h.upload.ImportRequest(**p))
        except Exception as ex:error={'type':type(ex).__name__,'message':str(ex)[:400]}
        assert response.status_code==500,(response.status_code,response.text)
        assert not h.stored(cid,t)
        return {'finding':'Nonfinite numeric input gives HTTP 500 instead of a field error','http':response.status_code,'body':response.text,'exception':error,'canonical_rows':0}
    h.check('Reproduce nonfinite amount failure and confirm no canonical writes',nonfinite)
    def final_protection():
        hashes=json.loads((h.PRIVATE/'original-hashes.json').read_text())
        assert all(hashlib.sha256(Path(p).read_bytes()).hexdigest()==v for p,v in hashes.items())
        return {'original_files_unchanged':len(hashes)}
    h.check('Final source and working-database protection checks',final_protection)


if __name__=='__main__':main()
