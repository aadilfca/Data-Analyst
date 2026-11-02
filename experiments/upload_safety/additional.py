"""Further opt-in cases; reuses only the isolated experiment harness."""
import concurrent.futures
import hashlib
import json
from pathlib import Path
import sys

import run as h
from fastapi import BackgroundTasks, HTTPException
from unittest.mock import patch
import openpyxl


def main():
    h.RESULTS.extend(json.loads((h.HERE/'results.json').read_text(encoding='utf-8')))
    docs = [
        ('Purchase Demand','2. purchase demands Test.xlsx', [('Document ID','Text','PD #'),('Transaction Date','Date','Date'),('Quantity','Numeric','Quantity')]),
        ('Purchase Order','3. Purchase Orders Test.xlsx', [('Document ID','Text','PO #'),('Transaction Date','Date','Date'),('Quantity','Numeric','Quantity'),('Amount','Numeric','Total Amount')]),
        ('Gate Pass','4. Gate Pass Test.xlsx', [('Document ID','Text','GP #'),('Transaction Date','Date','Date'),('Quantity','Numeric','Quantity')]),
        ('GRN','5. GRN Test.xlsx', [('Document ID','Text','GRN #'),('Transaction Date','Date','Receiving Date'),('Quantity','Numeric','Quantity'),('Amount','Numeric','Amount')]),
        ('Vendor Bill','6. Vendor bills Test.xlsx', [('Document ID','Text','Bill #'),('Transaction Date','Date','Date'),('Quantity','Numeric','Qty'),('Amount','Numeric','Amount')]),
        ('Extended Purchase Order','2. PURCHASE ORDER EXTENDED VIEW (JAN-JUNE).xlsx', [('Document ID','Text','PO #'),('Transaction Date','Date','PO Date'),('Quantity','Numeric','Qty'),('Amount','Numeric','Total')]),
        ('Extended Vendor Bill','5. SUPPLIERS BILLING EXTENDED VIEW (JAN-AUG).xlsx', [('Document ID','Text','Bill #'),('Transaction Date','Date','Date'),('Quantity','Numeric','Qty'),('Amount','Numeric','Total')]),
    ]
    for name,file,fields in docs:
        def case(name=name,file=file,fields=fields):
            path=h.ROOT/'uploads'/file;cid=h.client(name)
            tid,cols=h.template(name,[(n,k) for n,k,_ in fields])
            mapping={n:src for n,_,src in fields}
            payload=h.stage(path,cid,tid,cols,mapping,'2026-01-01','2026-12-31')
            result=h.validate(payload)
            h.apply(payload,result)
            _,headers,data=h.rows(path)
            ix={n:headers.index(s) for n,s in mapping.items()}
            good=[r for r in data if r[ix['Document ID']] and r[ix['Transaction Date']]]
            actual=h.stored(cid,tid)
            assert len(actual)==len(good),(len(actual),len(good))
            from collections import Counter
            expected_ids=Counter(str(r[ix['Document ID']]) for r in good)
            assert Counter(str(r['row_data']['Document ID']) for r in actual)==expected_ids
            totals={}
            for n,k,_ in fields:
                if k=='Numeric':
                    expected=sum(h.number(r[ix[n]]) for r in good)
                    got=sum(h.number(r['row_data'][n]) for r in actual)
                    assert abs(expected-got)<h.Decimal('0.00001'),(n,expected,got)
                    totals[n]=str(expected)
            again=h.validate(h.stage(path,cid,tid,cols,mapping,'2026-01-01','2026-12-31'))
            assert again['duplicate_upload_noop'] and not again['can_import'],again
            return {'rows':len(good),'numeric_totals':totals,'repeat_noop':True,'mapped_columns':mapping}
        h.check('Document '+name+' import, totals and repeat',case)
    base=h.PRIVATE/'fixtures'/'voucher-base.xlsx'
    _,headers,source=h.rows(base)
    names=['Voucher ID','Transaction Date','Account Detail','Account Name','Voucher Detail','Debit Amount','Credit Amount']
    kinds=['Text','Date','Text','Text','Text','Numeric','Numeric']
    tid,cols=h.template('Additional voucher',list(zip(names,kinds)))
    mapping=dict(zip(names,headers))
    def new_payload(path=base,cid=None,**options):
        return {**h.stage(path,cid or h.client('additional'),tid,cols,mapping,'2026-01-01','2026-01-31'),**options}
    def multiplicity():
        path=h.variant(base,'legitimate-repeated-line',lambda s:s.append([c.value for c in s[2]]))
        p=new_payload(path);v=h.validate(p);h.apply(p,v)
        assert len(h.stored(p['client_id'],tid))==len(source)+1
        again=h.validate(new_payload(path,p['client_id']))
        assert again['duplicate_upload_noop'],again
        return {'legitimate_occurrences_preserved':len(source)+1,'repeat_noop':True}
    h.check('Legitimate identical posting lines preserved without repeat-upload growth',multiplicity)
    def explicit_ids():
        def edit(s):
            s.cell(1,8).value='Line No'
            for i in range(2,s.max_row+1):s.cell(i,8).value='1' if i<4 else str(i)
            if s.max_row<3:
                s.append([c.value for c in s[2]])
        path=h.variant(base,'duplicate-line-id',edit)
        t,c=h.template('Explicit line identifiers',list(zip(names,kinds))+[('Line No','Text')])
        p=h.stage(path,h.client('explicit ids'),t,c,{**mapping,'Line No':'Line No'},'2026-01-01','2026-01-31')
        return h.reject(p,'duplicate')
    h.check('Repeated explicit line identifier is blocked',explicit_ids)
    def concurrency():
        p=new_payload();v=h.validate(p)
        request=h.upload.ImportRequest(**{**p,'validation_plan_id':v['validation_plan_id']})
        def send():
            try:return h.upload.validate_and_import(request,background_tasks=BackgroundTasks())
            except HTTPException as error:return {'http':error.status_code,'message':error.detail}
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(lambda _:send(),range(2)))
        assert any(r.get('status')=='success' for r in results),results
        assert len(h.stored(p['client_id'],tid))==len(source),results
        return {'responses':results,'stored_rows':len(source)}
    h.check('Simultaneous imports of one plan do not duplicate rows',concurrency)
    def partial():
        p=new_payload();h.apply(p,h.validate(p))
        path=h.variant(base,'partial-correction',lambda s:(s.delete_rows(3,max(1,s.max_row-2)),setattr(s.cell(2,5),'value','Partial correction experiment')))
        cp=new_payload(path,p['client_id'],comparison_mode='correction',coverage_start=None,coverage_end=None)
        result=h.apply(cp,h.validate(cp))
        assert result['deleted_count']==0 and len(h.stored(p['client_id'],tid))==len(source),result
        return {'retained_rows':len(source),'updated':result['updated_count'],'removed':result['deleted_count']}
    h.check('Partial correction retains absent voucher lines',partial)
    def complete_removal():
        p=new_payload();h.apply(p,h.validate(p))
        path=h.variant(base,'complete-removal',lambda s:s.delete_rows(s.max_row,1))
        cp=new_payload(path,p['client_id']);v=h.validate(cp)
        result=h.apply(cp,v)
        assert result['deleted_count']==1 and len(h.stored(p['client_id'],tid))==len(source)-1,result
        assert result['archive_verified'],result
        return {'removed':1,'archive_verified':True}
    h.check('Complete report removes only missing declared-period lines with archive',complete_removal)
    def missing_stage():
        p=new_payload();stage=h.query('SELECT file_path FROM uploaded_files WHERE id=%s',(p['file_id'],))[0]['file_path']
        Path(stage).unlink()
        return h.reject(p,'staged file is missing')
    h.check('Missing staged source gives recoverable error',missing_stage)
    def tampered():
        changed=h.variant(base,'tamper-source',lambda s:setattr(s.cell(2,5),'value','Before validation'))
        p=new_payload(changed);v=h.validate(p)
        path=Path(h.query('SELECT file_path FROM uploaded_files WHERE id=%s',(p['file_id'],))[0]['file_path'])
        path.write_bytes(path.read_bytes()+b'experiment alteration')
        before=h.fingerprint(p['client_id'],tid)
        try:h.apply(p,v)
        except HTTPException as error:
            assert error.status_code==409 and h.fingerprint(p['client_id'],tid)==before
            return {'http':error.status_code,'message':error.detail,'unchanged':True}
        raise AssertionError('Tampered source imported')
    h.check('Changed source after validation cannot import',tampered)
    def archive_failure():
        p=new_payload();h.apply(p,h.validate(p));before=h.fingerprint(p['client_id'],tid)
        path=h.variant(base,'archive-failure',lambda s:setattr(s.cell(2,5),'value','Archive failure experiment'))
        cp=new_payload(path,p['client_id']);v=h.validate(cp)
        from services import upload_lifecycle
        with patch.object(upload_lifecycle,'archive_directory',side_effect=RuntimeError('Injected archive unavailable')):
            try:h.apply(cp,v)
            except Exception:pass
            else:raise AssertionError('Archive failure did not block import')
        assert h.fingerprint(p['client_id'],tid)==before
        result=h.apply(cp,v)
        assert result['archive_verified']
        return {'failure_left_data_unchanged':True,'same_plan_retry':True}
    h.check('Archive failure blocks replacement; same plan can retry',archive_failure)
    def empty_report():
        path=h.variant(base,'empty-report',lambda s:s.delete_rows(2,s.max_row))
        p=new_payload();h.apply(p,h.validate(p));before=len(h.stored(p['client_id'],tid))
        cp=new_payload(path,p['client_id']);blocked=h.reject(cp)
        cp['confirm_empty_period']=True;result=h.apply(cp,h.validate(cp))
        assert not h.stored(p['client_id'],tid) and result['deleted_count']==before,result
        return {'confirmation_required':True,'removed':before,'archive_verified':result['archive_verified']}
    h.check('Empty report requires confirmation and clears only declared coverage',empty_report)
    def intact():
        hashes=json.loads((h.PRIVATE/'original-hashes.json').read_text())
        assert all(hashlib.sha256(Path(p).read_bytes()).hexdigest()==value for p,value in hashes.items())
        return {'unchanged_files':len(hashes)}
    h.check('Final original code, workbooks and working database checksum check',intact)


if __name__=='__main__':
    main()
