"""Opt-in real-workbook upload experiments. Never connects to the working app DB."""
from pathlib import Path
import os
import sys

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
PRIVATE = HERE / 'private'
# Set before database.py loads .env; python-dotenv does not override these.
DSN = 'postgresql://upload_test@127.0.0.1:55439/upload_safety_experiment'
os.environ['DATABASE_URL'] = DSN
os.environ['PG_POOL_MIN'] = '1'
os.environ['PG_POOL_MAX'] = '5'
os.environ['UPLOAD_STAGING_DIR'] = str(PRIVATE / 'staging')
os.environ['UPLOAD_ARCHIVE_TO_DRIVE'] = 'false'
os.environ['PYTHONIOENCODING'] = 'utf-8'
sys.path.insert(0, str(ROOT))
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')

import database
database.DB_NAME = str(PRIVATE / 'audit-only.sqlite')

import calendar
from collections import Counter
from contextlib import ExitStack
from datetime import datetime, date
from decimal import Decimal
import hashlib
import json
import shutil
import tempfile
import traceback
from unittest.mock import patch

import openpyxl
from fastapi import FastAPI, BackgroundTasks, HTTPException
from fastapi.testclient import TestClient
from routers import import_export as upload

query = database.execute_query_pg
write = database.execute_write_pg
RESULTS = []
ARCHIVES = tempfile.mkdtemp(prefix='upload-safety-archives-')
os.environ['UPLOAD_ARCHIVE_DIR'] = ARCHIVES
app = FastAPI()
app.include_router(upload.router)
app.dependency_overrides[upload.get_current_user] = lambda: {'username': 'Upload experiment'}
http = TestClient(app, raise_server_exceptions=False)


def check(name, function):
    started = __import__('time').perf_counter()
    try:
        detail = function() or {}
        result = dict(name=name, status='PASS', detail=detail)
    except Exception as error:
        result = dict(name=name, status='FAIL', error=str(error), traceback=traceback.format_exc())
    result['seconds'] = round(__import__('time').perf_counter() - started, 2)
    RESULTS.append(result)
    (HERE / 'results.json').write_text(json.dumps(RESULTS, indent=2, default=str), encoding='utf-8')
    print(result['status'], name, result.get('error', ''), flush=True)
    return result


def client(label):
    return write('INSERT INTO clients(client_code,client_name) VALUES (%s,%s) RETURNING id',
                 ('EXP-' + __import__('uuid').uuid4().hex, 'Upload experiment ' + label))


def template(label, columns, ledger=False):
    tid = write('INSERT INTO templates(template_name,data_purpose) VALUES (%s,%s) RETURNING id',
                ('Experiment ' + label + ' ' + __import__('uuid').uuid4().hex[:8], 'account_ledger' if ledger else 'standard'))
    cols = []
    for position, (name, kind) in enumerate(columns):
        found = query('SELECT id FROM master_columns WHERE column_name=%s', (name,))
        cid = found[0]['id'] if found else write('INSERT INTO master_columns(column_name,data_type) VALUES (%s,%s) RETURNING id', (name, kind))
        write('INSERT INTO template_column_mappings(template_id,column_id,sort_order) VALUES (%s,%s,%s)', (tid, cid, position))
        cols.append({'id': cid, 'column_name': name, 'data_type': kind})
    if ledger:
        write('UPDATE templates SET ledger_columns=%s WHERE id=%s',
              (json.dumps(dict(zip(['voucher','date','description','debit','credit','balance'], [c['id'] for c in cols]))), tid))
    return tid, cols


def rows(path):
    book = openpyxl.load_workbook(path, read_only=True, data_only=True)
    sheet = book.worksheets[0]
    data = list(sheet.values)
    title = sheet.title
    book.close()
    return title, data[0], data[1:]


def stage(path, cid, tid, cols, mapping, start, end, account=None):
    response = http.post('/api/import-excel', files={'file': (path.name, path.read_bytes(), 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')})
    assert response.status_code == 200, response.text
    file_id = response.json()['file_id']
    title, _, _ = rows(path)
    payload = dict(file_id=file_id, client_id=cid, template_id=tid, sheet_name=title,
                   comparison_mode='replace_range', coverage_start=start, coverage_end=end,
                   date_delimiter='/', date_order='DMY',
                   mappings=[dict(template_column_id=c['id'], template_column_name=c['column_name'], excel_column_name=mapping[c['column_name']]) for c in cols])
    if account:
        payload.update(ledger_account_code=account, ledger_period_start=start, ledger_period_end=end)
    return payload


def validate(payload):
    response = http.post('/api/upload/validate', json=payload)
    assert response.status_code == 200, response.text
    return response.json()


def apply(payload, validation):
    assert validation.get('can_import'), json.dumps(validation, default=str)[:1300]
    request = upload.ImportRequest(**{**payload, 'validation_plan_id': validation['validation_plan_id']})
    # Execute the unchanged import handler and SQL transaction; leave unrelated
    # report-preparation background jobs queued rather than running reports.
    result = upload.validate_and_import(request, background_tasks=BackgroundTasks())
    assert result['status'] == 'success', result
    return result


def stored(cid, tid):
    return query('SELECT id,row_data,extract_date,source_identity FROM client_data WHERE client_id=%s AND template_id=%s AND is_deleted=FALSE ORDER BY id', (cid, tid))


def fingerprint(cid, tid):
    return hashlib.sha256(json.dumps(stored(cid, tid), sort_keys=True, default=str).encode()).hexdigest()


def number(value):
    return Decimal(str(value or 0).replace(',', ''))


def independent_source(path, mapping):
    _, headers, data = rows(path)
    index = {name: headers.index(source) for name, source in mapping.items()}
    expected = []
    dates = []
    for row in data:
        key = row[index['Voucher ID']]
        day = row[index['Transaction Date']]
        if not key or not day:
            continue
        if isinstance(day, datetime):
            parsed = day.date()
        elif isinstance(day, date):
            parsed = day
        else:
            parsed = datetime.strptime(str(day).strip(), '%d/%m/%Y').date()
        expected.append({name: row[pos] for name, pos in index.items()})
        dates.append(parsed.isoformat())
    return expected, min(dates), max(dates)


def assert_source(path, cid, tid, mapping):
    expected, start, end = independent_source(path, mapping)
    actual = stored(cid, tid)
    # Compare occurrences, not just distinct voucher IDs: multi-line vouchers
    # and identical legitimate posting lines must all survive.
    def signature(raw, day):
        return (str(raw['Voucher ID']), day,
                str(raw.get('Account Detail', '')),
                number(raw.get('Debit Amount')), number(raw.get('Credit Amount')))
    exp = Counter(signature(r, (r['Transaction Date'].date().isoformat() if isinstance(r['Transaction Date'], datetime)
                               else datetime.strptime(str(r['Transaction Date']), '%d/%m/%Y').date().isoformat())) for r in expected)
    got = Counter(signature(r['row_data'], str(r['extract_date'])) for r in actual)
    assert exp == got, f'Source/store mismatch: expected {len(expected)}, stored {len(actual)}; missing {sum((exp-got).values())}, extra {sum((got-exp).values())}'
    assert len({r['source_identity'] for r in actual}) == len(actual), 'Duplicate stored identities'
    return {'source_rows':len(expected), 'stored_rows':len(actual), 'start': start, 'end': end,
            'debit': str(sum(number(r.get('Debit Amount')) for r in expected)),
            'credit':str(sum(number(r.get('Credit Amount')) for r in expected))}


def variant(source, name, edit):
    path = PRIVATE / 'fixtures' / (name + '.xlsx')
    path.parent.mkdir(parents=True, exist_ok=True)
    # Freeze cached formula results in experiment copies. openpyxl cannot
    # recalculate formulas and saving formula cells would discard their cache.
    book = openpyxl.load_workbook(source, data_only=True)
    edit(book.worksheets[0])
    book.save(path)
    book.close()
    return path


def reject(payload, expected_text=None):
    before = fingerprint(payload['client_id'],payload['template_id'])
    response = http.post('/api/upload/validate', json=payload)
    detail = response.json()
    blocked = response.status_code >= 400 or not detail.get('can_import')
    assert blocked, 'Invalid source unexpectedly eligible for import'
    assert before == fingerprint(payload['client_id'],payload['template_id']), 'Validation changed canonical data'
    assert not detail.get('validation_plan_id'), 'Invalid source received a plan'
    if expected_text:
        assert expected_text.casefold() in json.dumps(detail).casefold(), detail
    return {'http': response.status_code, 'response':detail, 'stored_data_unchanged':True}


def run():
    # Legacy audit writes also go to a private empty SQLite DB, never test.db.
    import sqlite3
    with sqlite3.connect(database.DB_NAME) as conn:
        conn.execute('CREATE TABLE IF NOT EXISTS audit_log(id INTEGER PRIMARY KEY,user TEXT,action TEXT,module TEXT,details TEXT)')
    tracked = [ROOT/'routers/import_export.py', ROOT/'database.py', ROOT/'static/upload.js', ROOT/'static/upload.html', ROOT/'services/upload_lifecycle.py']
    sources = sorted((ROOT/'uploads').glob('*.xlsx'))
    protected = tracked + sources + ([ROOT/'test.db'] if (ROOT/'test.db').exists() else [])
    hashes = {str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in protected}
    (PRIVATE/'original-hashes.json').write_text(json.dumps(hashes,indent=2), encoding='utf-8')
    voucher_cols = [('Voucher ID','Text'),('Transaction Date','Date'),('Account Detail','Text'),('Account Name','Text'),('Voucher Detail','Text'),('Debit Amount','Numeric'),('Credit Amount','Numeric')]
    vt, vc = template('Voucher',voucher_cols)
    all_months = client('all months')
    for month, prefix in enumerate(['6.','7.','8.','9.','10.','11.','12.','13.'],1):
        path = next(p for p in sources if p.name.startswith(prefix+' VOUCHER TRANSACTIONS'))
        _, headers, _ = rows(path)
        mapping = dict(zip([c['column_name'] for c in vc], headers[:7]))
        cid = client('month '+str(month))
        start = f'2026-{month:02}-01'; end = f'2026-{month:02}-{calendar.monthrange(2026,month)[1]}'
        def monthly(path=path,cid=cid,mapping=mapping,start=start,end=end):
            payload=stage(path,cid,vt,vc,mapping,start,end)
            result=validate(payload); apply(payload,result)
            proof=assert_source(path,cid,vt,mapping)
            before=fingerprint(cid,vt)
            repeat=validate(stage(path,cid,vt,vc,mapping,start,end))
            assert repeat['duplicate_upload_noop'] and not repeat['can_import'], repeat
            assert fingerprint(cid,vt)==before
            proof['repeat_noop']=True
            # Same reports must accumulate into a single client without overlap.
            combined=stage(path,all_months,vt,vc,mapping,start,end)
            apply(combined,validate(combined))
            proof['cumulative_rows']=len(stored(all_months,vt))
            return proof
        check('Voucher month '+str(month)+' import, totals, repeat and accumulation',monthly)
    # Account ledger sources use the actual six source columns and synthetic
    # current Trial Balance posting accounts; no existing client settings copied.
    lt,lc=template('Account Ledger',[(n,k) for n,k in voucher_cols if n not in ('Account Detail','Account Name')]+[('Balance','Text')],True)
    ledgers=[]
    for term in ['Daska ledger.xlsx','Main Cash in Hand.xlsx','Online Cashbook.xlsx','Meezan Bank.xlsx','Sirat Bank.xlsx','Bank Albaraka.xlsx']:
        candidates=[p for p in sources if p.name.endswith(term)]
        if candidates:
            ledgers.append(candidates[0])
    for i,path in enumerate(ledgers):
        cid=client('ledger '+str(i)); account='EXP-ACCOUNT-'+str(i)
        tb=write("INSERT INTO trial_balance_uploads(client_id,as_of_date,file_name,status,period_end) VALUES (%s,'2026-12-31','Synthetic test accounts','parsed','2026-12-31') RETURNING id",(cid,))
        write("INSERT INTO trial_balance_accounts(upload_id,account_code,account_name,classification) VALUES (%s,%s,%s,'leaf')",(tb,account,'Test posting account'))
        mapping=dict(zip([c['column_name'] for c in lc],['Voucher ID','Date','Description','Debit','Credit','Balance']))
        _,start,end=independent_source(path,mapping)
        def ledger_test(path=path,cid=cid,account=account,mapping=mapping,start=start,end=end):
            payload=stage(path,cid,lt,lc,mapping,start,end,account)
            result=validate(payload); imported=apply(payload,result)
            proof=assert_source(path,cid,lt,mapping)
            meta=query('SELECT ledger_evidence,ledger_account_code FROM uploaded_files WHERE id=%s',(payload['file_id'],))[0]
            proof['evidence']=meta
            assert meta['ledger_account_code']==account
            before=fingerprint(cid,lt)
            again=validate(stage(path,cid,lt,lc,mapping,start,end,account))
            assert again['duplicate_upload_noop'] and not again['can_import'], again
            assert fingerprint(cid,lt)==before
            proof['repeat_noop']=True
            reject({**stage(path,cid,lt,lc,mapping,start,end,account),'ledger_account_code':None},'posting account')
            reject({**stage(path,cid,lt,lc,mapping,start,end,account),'ledger_account_code':'UNKNOWN'},'latest Trial Balance')
            return proof
        check('Ledger '+str(i+1)+' import, balances, repeat and account guards',ledger_test)
    # Small test workbook drawn from complete real vouchers for adversarial cases.
    base=next(p for p in sources if p.name.startswith('6. VOUCHER TRANSACTIONS'))
    _,headers,data=rows(base)
    first=data[0][0]
    tiny=PRIVATE/'fixtures'/'voucher-base.xlsx'; tiny.parent.mkdir(parents=True,exist_ok=True)
    book=openpyxl.Workbook(); sheet=book.active; sheet.append(headers)
    for r in data:
        if r[0]==first: sheet.append(r)
    book.save(tiny);book.close()
    mapping=dict(zip([c['column_name'] for c in vc],headers[:7]))
    cid=client('adversarial'); _,start,end=independent_source(tiny,mapping)
    def payload_for(path=tiny,**kw):
        return {**stage(path,cid,vt,vc,mapping,start,end),**kw}
    baseline=payload_for(); check('Adversarial baseline import',lambda:apply(baseline,validate(baseline)))
    for name,col,value in [('invalid-date',2,'31/02/2026'),('blank-date',2,None),('blank-voucher',1,None),('invalid-debit',6,'abc'),('blank-debit-credit',6,None),('negative-debit',6,-100),('nonfinite-debit',6,'Infinity'),('excel-error',6,'#VALUE!')]:
        def case(name=name,col=col,value=value):
            def edit(s):
                s.cell(2,col,value) if value is not None else setattr(s.cell(2,col),'value',None)
                if name=='blank-debit-credit': s.cell(2,7).value=None
            return reject(payload_for(variant(tiny,name,edit)))
        check('Reject '+name,case)
    check('Reject missing column mapping',lambda:reject({**payload_for(),'mappings':baseline['mappings'][:-1]}))
    check('Reject reversed coverage',lambda:reject(payload_for(coverage_start='2026-02-28',coverage_end='2026-01-01')))
    check('Reject outside coverage',lambda:reject(payload_for(coverage_start='2027-01-01',coverage_end='2027-01-31')))
    check('Reject wrong worksheet',lambda:reject(payload_for(sheet_name='NO SUCH SHEET')))
    check('Reject populated report marked empty',lambda:reject(payload_for(confirm_empty_period=True)))
    for name,content in [('empty.xlsx',b''),('corrupt.xlsx',b'not a workbook'),('web.xlsx',b'<html><table><tr><td>data</td></tr></table></html>'),('wrong.csv',b'a,b\n1,2')]:
        def badfile(name=name,content=content):
            before=query('SELECT count(*) AS n FROM uploaded_files')[0]['n']
            response=http.post('/api/import-excel',files={'file':(name,content)})
            assert response.status_code==400,response.text
            assert query('SELECT count(*) AS n FROM uploaded_files')[0]['n']==before
            return response.json()
        check('Reject file '+name,badfile)
    def repeat_renamed():
        renamed=PRIVATE/'fixtures'/'renamed.xlsx';shutil.copy2(tiny,renamed)
        result=validate(payload_for(renamed))
        assert not result['can_import'] and result['duplicate_upload_noop'],result
        return {'repeat_noop':True}
    check('Renamed identical workbook is a no-op',repeat_renamed)
    def stale():
        changed=variant(tiny,'correction',lambda s:setattr(s.cell(2,5),'value','Experiment revised description'))
        p=payload_for(changed);v=validate(p)
        assert v['can_import'],v
        query_plan=query('SELECT plan_id FROM upload_validation_plans WHERE plan_id=%s',(v['validation_plan_id'],))
        assert query_plan
        write("UPDATE upload_validation_plans SET expires_at=now()-interval '1 minute' WHERE plan_id=%s",(v['validation_plan_id'],))
        before=fingerprint(cid,vt)
        try: apply(p,v)
        except HTTPException as e:
            assert e.status_code==409 and before==fingerprint(cid,vt)
            return {'http':e.status_code,'message':e.detail,'stored_data_unchanged':True}
        raise AssertionError('Expired plan applied')
    check('Expired validation plan cannot import',stale)
    def correction():
        path=PRIVATE/'fixtures'/'correction.xlsx';p=payload_for(path);v=validate(p)
        before=len(stored(cid,vt));result=apply(p,v)
        assert len(stored(cid,vt))==before and result['updated_count']==1,result
        assert result['archive_verified'],result
        return {'rows':before,'updated':result['updated_count'],'archive_verified':True}
    check('Correction updates once with verified local archive',correction)
    def rollback():
        changed=variant(tiny,'rollback',lambda s:setattr(s.cell(2,5),'value','Experiment interrupted correction'))
        p=payload_for(changed);v=validate(p);before=fingerprint(cid,vt)
        with patch.object(upload,'upsert_classification_rows_postgres',side_effect=RuntimeError('Injected post-write failure')):
            try: apply(p,v)
            except Exception: pass
            else: raise AssertionError('Injected failure did not occur')
        assert fingerprint(cid,vt)==before,'Partial writes survived failure'
        result=apply(p,v)
        assert result['updated_count']==1,result
        before=fingerprint(cid,vt);retry=apply(p,v)
        assert fingerprint(cid,vt)==before and retry['status']=='success'
        return {'rollback':True,'same_plan_retry':True,'receipt_retry_noop':True}
    check('Post-write failure rolls back; retry and receipt remain safe',rollback)
    def uniqueness():
        row=stored(cid,vt)[0]
        try:
            with database.pg_transaction():
                write('INSERT INTO client_data(client_id,template_id,upload_id,row_data,source_identity) SELECT client_id,template_id,upload_id,row_data,source_identity FROM client_data WHERE id=%s',(row['id'],))
        except Exception as error:
            assert getattr(error,'pgcode',None)=='23505',str(error)
            return {'unique_constraint_blocks_duplicate':True}
        raise AssertionError('Database allowed duplicate identity')
    check('Database unique identity rejects duplicate insert',uniqueness)
    def unchanged():
        changed=[p for p,h in hashes.items() if hashlib.sha256(Path(p).read_bytes()).hexdigest()!=h]
        assert not changed,changed
        return {'unchanged_files':len(hashes),'includes_working_sqlite':str(ROOT/'test.db') in hashes}
    check('Original code, all source workbooks and working SQLite unchanged',unchanged)
    print('Results:',HERE/'results.json',flush=True)


def bootstrap():
    import psycopg2
    with psycopg2.connect(DSN) as conn:
        with conn.cursor() as cur:
            cur.execute('SELECT current_database(), inet_server_addr()::text, inet_server_port()')
            assert cur.fetchone() == ('upload_safety_experiment', '127.0.0.1/32', 55439)
        database.init_db_postgres(conn)
    print('Isolated PostgreSQL schema ready', flush=True)


if __name__ == '__main__':
    if '--bootstrap' in sys.argv:
        bootstrap()
    elif '--regression' in sys.argv:
        # Existing fixtures model verified Drive receipts; preserve that test
        # contract independently of this experiment's local-only archive mode.
        os.environ['UPLOAD_ARCHIVE_TO_DRIVE'] = 'true'
        import pytest
        sys.exit(pytest.main(['tests/test_import_duplicate_gates.py',
                              'tests/test_account_ledger_foundation.py',
                              'tests/test_upload_lifecycle.py',
                              'tests/test_upload_observation_report.py',
                              '-q', '-p', 'no:cacheprovider',
                              '--basetemp', tempfile.mkdtemp(prefix='upload-safety-pytest-')]))
    else:
        run()
