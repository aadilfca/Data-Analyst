"""Opt-in verification of upload repairs; uses only the isolated loopback DB.

Baseline experiment evidence remains intact. Results are private and do not
include working client data. Background report preparation remains out of scope.
"""
import ast
import calendar
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import time
from unittest.mock import patch

import run as h


RESULTS = []


def check(name, action):
    started = time.perf_counter()
    try:
        result = dict(name=name, status='PASS', detail=action())
    except Exception as error:
        import traceback
        result = dict(name=name, status='FAIL', error=str(error), traceback=traceback.format_exc())
    result['seconds'] = round(time.perf_counter()-started, 2)
    RESULTS.append(result)
    (h.PRIVATE/'after-fixes-results.json').write_text(json.dumps(RESULTS, indent=2, default=str), encoding='utf-8')
    print(result['status'], name, result.get('error',''), flush=True)


def duplicate_policy():
    before = subprocess.check_output(['git','show','HEAD:routers/import_export.py'], cwd=h.ROOT).decode('utf-8')
    after = (h.ROOT/'routers/import_export.py').read_text(encoding='utf-8')
    names=['_canonical_source_identity','_canonical_source_identities','_deduplicate_exact_rows',
           '_strict_duplicate_row_errors','_assert_active_identity_collisions','compare_transactions',
           '_legacy_compare_transactions','get_document_group_id','_is_duplicate_upload_noop',
           '_row_comparison_fingerprint','_comparison_line_key','_normalized_key_value',
           '_find_prior_import_with_same_hash','_validation_request_signature']
    def functions(source):
        return {n.name:ast.dump(n,include_attributes=False) for n in ast.parse(source).body
                if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef)) and n.name in names}
    assert functions(before)==functions(after)
    assert len(functions(after))==len(names)
    return {'unchanged_policy_functions':len(names)}


def main():
    if '--inputs' in sys.argv or '--quick' in sys.argv:
        RESULTS.extend(json.loads((h.PRIVATE/'after-fixes-results.json').read_text(encoding='utf-8')))
    actual = h.query('SELECT current_database() AS db,inet_server_addr()::text AS host,inet_server_port() AS port')[0]
    assert actual==dict(db='upload_safety_experiment',host='127.0.0.1/32',port=55439),actual
    check('Duplicate and comparison policy functions unchanged',duplicate_policy)
    assert h.query("SELECT to_regclass('public.cashflow_ledger_rollup_read_models') AS legacy")[0]['legacy'] is None
    names=['Voucher ID','Transaction Date','Account Detail','Account Name','Voucher Detail','Debit Amount','Credit Amount']
    kinds=['Text','Date','Text','Text','Text','Numeric','Numeric']
    tid,cols=h.template('After fixes voucher',list(zip(names,kinds)))
    base=h.PRIVATE/'fixtures'/'voucher-base.xlsx'
    _,headers,source=h.rows(base)
    mapping=dict(zip(names,headers))
    def payload(path=base,cid=None,**changes):
        return {**h.stage(path,cid or h.client('after fixes'),tid,cols,mapping,'2026-01-01','2026-01-31'),**changes}
    def invalid(value,column):
        path=h.variant(base,'after-invalid-'+str(column)+'-'+str(value).replace('/','_'),
                       lambda sheet:setattr(sheet.cell(2,column),'value',value))
        p=payload(path);response=h.http.post('/api/upload/validate',json=p)
        assert response.status_code==200,response.text
        result=response.json()
        assert not result['can_import'] and result['errors'][0]['row_number']==2,result
        assert result['errors'][0]['errors'][0]['column']==names[column-1],result
        assert not h.stored(p['client_id'],tid)
        assert not h.query('SELECT plan_id FROM upload_validation_plans WHERE file_id=%s',(p['file_id'],))
        # Complete reports already reject imports without a plan. Exercise the
        # supported direct partial-extract path to verify the cell gate as well.
        direct_payload={**p,'comparison_mode':'correction','coverage_start':None,'coverage_end':None}
        direct=h.upload.validate_and_import(h.upload.ImportRequest(**direct_payload),background_tasks=h.BackgroundTasks())
        assert direct['status']=='validation_failed' and not h.stored(p['client_id'],tid)
        return {'message':result['errors'][0]['errors'][0]['message'],'canonical_rows':0,'no_plan_created':True}
    for value,column in [('Infinity',6),('-Infinity',6),('NaN',6),('1e9999',6),('#REF!',4),('#VALUE!',6)]:
        check('Malformed source '+str(value)+' column '+str(column),lambda value=value,column=column:invalid(value,column))
    if '--inputs' in sys.argv:
        latest={r['name']:r for r in RESULTS}
        sys.exit(1 if any(r['status']=='FAIL' for r in latest.values()) else 0)
    def replacement():
        p=payload();h.apply(p,h.validate(p));before=h.stored(p['client_id'],tid)
        shorter=h.variant(base,'after-removal',lambda sheet:sheet.delete_rows(sheet.max_row,1))
        cp=payload(shorter,p['client_id']);v=h.validate(cp)
        result=h.apply(cp,v)
        assert result['deleted_count']==1 and result['archive_verified'],result
        assert len(h.stored(p['client_id'],tid))==len(before)-1
        digest=h.fingerprint(p['client_id'],tid)
        retry=h.apply(cp,v)
        assert retry['status']=='success' and h.fingerprint(p['client_id'],tid)==digest
        return {'removed_rows':1,'archive_verified':True,'receipt_retry_noop':True,'retired_table_absent':True}
    check('Replacement removal works on current fresh-bootstrap schema',replacement)
    def empty():
        p=payload();h.apply(p,h.validate(p));count=len(h.stored(p['client_id'],tid))
        path=h.variant(base,'after-empty',lambda sheet:sheet.delete_rows(2,sheet.max_row))
        cp=payload(path,p['client_id']);h.reject(cp)
        cp['confirm_empty_period']=True
        result=h.apply(cp,h.validate(cp))
        assert result['deleted_count']==count and result['archive_verified']
        assert not h.stored(p['client_id'],tid)
        return {'empty_confirmation_required':True,'removed':count,'archive_verified':True}
    check('Confirmed empty-period replacement works with archive',empty)
    def repeats():
        p=payload();h.apply(p,h.validate(p));before=h.fingerprint(p['client_id'],tid)
        renamed=h.PRIVATE/'fixtures'/'after-renamed.xlsx';shutil.copy2(base,renamed)
        repeat=h.validate(payload(renamed,p['client_id']))
        assert repeat['duplicate_upload_noop'] and not repeat['can_import']
        assert before==h.fingerprint(p['client_id'],tid)
        return {'renamed_repeat_noop':True}
    check('Renamed repeat still inserts nothing',repeats)
    def multiplicity():
        copy=h.variant(base,'after-identical-lines',lambda sheet:sheet.append([c.value for c in sheet[2]]))
        p=payload(copy);h.apply(p,h.validate(p));actual=h.stored(p['client_id'],tid)
        assert len(actual)==len(source)+1 and len({r['source_identity'] for r in actual})==len(actual)
        repeat=h.validate(payload(copy,p['client_id']));assert repeat['duplicate_upload_noop']
        return {'legitimate_lines':len(actual),'repeat_noop':True}
    check('Legitimate identical lines retain their multiplicity',multiplicity)
    def concurrent():
        p=payload();v=h.validate(p)
        def send():
            try:return h.apply(p,v)
            except h.HTTPException as error:return {'http':error.status_code}
        with ThreadPoolExecutor(max_workers=2) as pool:
            replies=list(pool.map(lambda _:send(),range(2)))
        assert any(r.get('status')=='success' for r in replies)
        assert len(h.stored(p['client_id'],tid))==len(source)
        return {'stored_rows':len(source),'simultaneous_retry_safe':True}
    check('Concurrent imports of one plan do not duplicate rows',concurrent)
    def duplicate_id():
        def edit(sheet):
            sheet.cell(1,8).value='Line No'
            for row in range(2,sheet.max_row+1):sheet.cell(row,8).value='1'
        copy=h.variant(base,'after-duplicate-ids',edit)
        t,c=h.template('After fixes explicit IDs',list(zip(names,kinds))+[('Line No','Text')])
        p=h.stage(copy,h.client('duplicate IDs after fixes'),t,c,{**mapping,'Line No':'Line No'},'2026-01-01','2026-01-31')
        result=h.validate(p)
        assert not result['can_import'] and result['has_errors']
        assert any(e['error_type']=='duplicate_row' for r in result['errors'] for e in r['errors'])
        assert not h.stored(p['client_id'],t)
        return {'duplicate_line_ids_blocked':True}
    check('Duplicate explicit line IDs remain blocked',duplicate_id)
    def rollback():
        p=payload();h.apply(p,h.validate(p));before=h.fingerprint(p['client_id'],tid)
        copy=h.variant(base,'after-replacement-rollback',lambda sheet:setattr(sheet.cell(2,5),'value','Changed description'))
        cp=payload(copy,p['client_id']);v=h.validate(cp)
        with patch.object(h.upload,'upsert_classification_rows_postgres',side_effect=RuntimeError('Injected post-write failure')):
            try:h.apply(cp,v)
            except RuntimeError:pass
            else:raise AssertionError('Failure injection did not execute')
        assert h.fingerprint(p['client_id'],tid)==before
        result=h.apply(cp,v)
        assert result['archive_verified'] and len(h.stored(p['client_id'],tid))==len(source)
        return {'actual_sql_rollback':True,'same_plan_retry_succeeds':True}
    check('Replacement failure rolls back; same plan can retry',rollback)
    if '--quick' in sys.argv:
        latest={r['name']:r for r in RESULTS}
        sys.exit(1 if any(r['status']=='FAIL' for r in latest.values()) else 0)
    for month,prefix in enumerate(['6.','7.','8.','9.','10.','11.','12.','13.'],1):
        def monthly(month=month,prefix=prefix):
            path=next((h.ROOT/'uploads').glob(prefix+' VOUCHER TRANSACTIONS*.xlsx'))
            _,hdr,_=h.rows(path);mp=dict(zip(names,hdr[:7]));cid=h.client('after fixes month '+str(month))
            start=f'2026-{month:02}-01';end=f'2026-{month:02}-{calendar.monthrange(2026,month)[1]}'
            p=h.stage(path,cid,tid,cols,mp,start,end)
            if month==6:
                rejected=h.validate(p)
                assert not rejected['can_import'] and any(r['row_number']==2 for r in rejected['errors'])
                # Demonstration correction only: never guess a real account name.
                path=h.variant(path,'after-june-formula-corrected',lambda sheet:setattr(sheet.cell(2,4),'value','Experiment corrected account name'))
                p=h.stage(path,cid,tid,cols,mp,start,end)
            h.apply(p,h.validate(p));proof=h.assert_source(path,cid,tid,mp)
            repeat=h.validate(h.stage(path,cid,tid,cols,mp,start,end))
            assert repeat['duplicate_upload_noop'] and not repeat['can_import']
            proof['repeat_noop']=True
            if month==6:proof['original_excel_error_blocked']=True
            return proof
        check('Real voucher month '+str(month)+' totals and repeat',monthly)
    def ledgers():
        t,c=h.template('After fixes ledger',[(n,k) for n,k in zip(names,kinds) if n not in ('Account Detail','Account Name')]+[('Balance','Text')],True)
        cid=h.client('after fixes account isolation')
        tb=h.write("INSERT INTO trial_balance_uploads(client_id,as_of_date,file_name,status,period_end) VALUES (%s,'2026-12-31','Synthetic accounts','parsed','2026-12-31') RETURNING id",(cid,))
        for account in ['AF-A','AF-B']:
            h.write("INSERT INTO trial_balance_accounts(upload_id,account_code,account_name,classification) VALUES (%s,%s,%s,'leaf')",(tb,account,'Synthetic '+account))
        path=next((h.ROOT/'uploads').glob('*Daska ledger.xlsx'))
        mp=dict(zip([column['column_name'] for column in c],['Voucher ID','Date','Description','Debit','Credit','Balance']))
        _,start,end=h.independent_source(path,mp)
        count=0
        for account in ['AF-A','AF-B']:
            p=h.stage(path,cid,t,c,mp,start,end,account);h.apply(p,h.validate(p))
            v=h.validate(h.stage(path,cid,t,c,mp,start,end,account));assert v['duplicate_upload_noop']
            count+=1
        records=h.stored(cid,t);expected=len(h.independent_source(path,mp)[0])*count
        assert len(records)==expected and len({r['source_identity'] for r in records})==expected
        for item in h.query("SELECT ledger_evidence FROM uploaded_files WHERE client_id=%s AND status='Imported'",(cid,)):
            evidence=json.loads(item['ledger_evidence']);assert evidence['balance_check']['difference_count']==0
        return {'accounts':2,'posting_rows':expected,'balance_differences':0,'repeat_noop':True}
    check('Real ledger balances and account scopes still hold',ledgers)
    def originals():
        original=json.loads((h.PRIVATE/'original-hashes.json').read_text())
        protected={p:value for p,value in original.items() if Path(p).parent==h.ROOT/'uploads' or Path(p)==h.ROOT/'test.db'}
        assert all(hashlib.sha256(Path(p).read_bytes()).hexdigest()==value for p,value in protected.items())
        return {'source_workbooks_and_working_sqlite_unchanged':len(protected)}
    check('Original workbooks and working database remain unchanged',originals)
    sys.exit(1 if any(r['status']=='FAIL' for r in RESULTS) else 0)


if __name__=='__main__':
    if '--tests' in sys.argv:
        import pytest
        h.os.environ['UPLOAD_ARCHIVE_TO_DRIVE']='true'
        sys.exit(pytest.main(['tests/test_upload_input_errors.py','tests/test_data_deletion.py',
            'tests/test_account_ledger_foundation.py','tests/test_upload_lifecycle.py',
            'tests/test_import_duplicate_gates.py','tests/test_upload_observation_report.py',
            '-q','-p','no:cacheprovider','--basetemp',h.tempfile.mkdtemp(prefix='upload-after-fixes-tests-')]))
    main()
