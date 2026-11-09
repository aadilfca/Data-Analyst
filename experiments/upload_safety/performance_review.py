"""Opt-in Folders 1–6 review. Mutations run only in this run's fresh loopback cluster.

Original ERP workbooks are read/copy sources. Working-app access is limited to
login and configuration/preview reads. Private copies/results never belong in Git.
"""
from __future__ import annotations

import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import date, datetime
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import signal
import subprocess
import sys
import time
import traceback
import uuid

import httpx
import openpyxl
import psycopg2
from psycopg2.extras import RealDictCursor

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
PRIVATE = HERE / 'private' / 'performance_review'
SOURCES = Path(r'D:\Excel Auto\Excel test files')
TOOLS = ROOT / 'LeatherCrop-Database-Backups/postgres17-rehearsal-20261019/tools/pgsql/bin'
SPEC = {
    1: ('Transaction Report', 'vouchers-3 Jan.xlsx', 'Vouchers Jan to 06 Nov.xlsx'),
    2: ('Purchase Order', 'PO 11.xlsx', 'PO Jan 06-Nov.xlsx'),
    3: ('Purchase Demand', 'PD 11-02.xlsx', 'PD Jan to 06-Nov.xlsx'),
    4: ('Gate Pass', 'GP 03-11.xlsx', 'GP Jan to 06-Nov.xlsx'),
    5: ('Store receiving', 'GRN 04-11.xlsx', 'GRN Jan to 06 Nov.xlsx'),
    6: ('Vendor Bill', 'Supplier billing 03 Nov.xlsx', 'Supplier billing Jan to 06-Nov.xlsx'),
}


def dump(path, value):
    path.write_text(json.dumps(value, indent=2, default=str), encoding='utf-8')


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def working_credentials():
    # Supplied by the operator; never persist working-login credentials in evidence.
    return dict(username=os.environ['PERFORMANCE_REVIEW_USERNAME'],
                password=os.environ['PERFORMANCE_REVIEW_PASSWORD'])


def files():
    for folder in sorted(SOURCES.iterdir()):
        if folder.is_dir() and re.match(r'^[1-6](?:\D|$)', folder.name):
            for path in sorted(folder.glob('*.xlsx')):
                if not path.name.startswith('~$'):
                    yield int(folder.name[0]), path


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def load_context():
    context_path = Path((PRIVATE / 'latest.txt').read_text().strip())
    assert context_path.resolve().is_relative_to(PRIVATE.resolve())
    return json.loads(context_path.read_text(encoding='utf-8'))


@contextmanager
def connection(ctx):
    conn = psycopg2.connect(host='127.0.0.1', hostaddr='127.0.0.1', port=ctx['pg_port'],
        dbname='upload_perf_review', user='upload_perf', connect_timeout=5, sslmode='disable')
    with conn.cursor() as cur:
        cur.execute("SELECT current_database(),inet_server_addr()::text,inet_server_port(),current_setting('data_directory')")
        name, host, port, directory = cur.fetchone()
        assert name == 'upload_perf_review' and host == '127.0.0.1/32' and port == ctx['pg_port']
        assert Path(directory).resolve() == (Path(ctx['work']) / 'cluster').resolve()
        assert (Path(ctx['work']) / 'marker').read_text() == ctx['marker']
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def query(ctx, sql, params=()):
    with connection(ctx) as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(sql, params)
        return [dict(row) for row in cur.fetchall()]


def write(ctx, sql, params=()):
    with connection(ctx) as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchone()[0] if cur.description else None


def isolated_environment(ctx):
    # Disable dotenv before importing any application module; do not inherit
    # ambient libpq/Drive configuration from a working application process.
    for key in list(os.environ):
        if key.upper().startswith(('PG', 'DRIVE_', 'UPLOAD_')):
            os.environ.pop(key, None)
    os.environ.update(DATABASE_URL=f"postgresql://upload_perf@127.0.0.1:{ctx['pg_port']}/upload_perf_review",
        PYTHON_DOTENV_DISABLED='1', PGSSLMODE='disable', PG_POOL_MIN='1', PG_POOL_MAX='5',
        UPLOAD_STAGING_DIR=str(Path(ctx['work']) / 'staging'), UPLOAD_ARCHIVE_TO_DRIVE='false',
        UPLOAD_ARCHIVE_DIR=str(Path(ctx['work']) / 'archives'), PYTHONIOENCODING='utf-8')
    import dotenv
    dotenv.load_dotenv = lambda *args, **kwargs: False
    sys.path.insert(0, str(ROOT))
    import database
    database.DB_NAME = str(Path(ctx['work']) / 'audit-only.db')
    return database


def tool(work, name, args):
    log = work / (name + '-' + uuid.uuid4().hex[:5] + '.log')
    with log.open('wb') as out:
        subprocess.run([str(TOOLS / (name + '.exe')), *map(str, args)], stdin=subprocess.DEVNULL,
            stdout=out, stderr=subprocess.STDOUT, check=True, timeout=120,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))


def setup():
    PRIVATE.mkdir(parents=True, exist_ok=True)
    work = PRIVATE / ('run-' + datetime.now().strftime('%Y%m%d-%H%M%S'))
    work.mkdir()
    ctx = dict(work=str(work), marker=uuid.uuid4().hex, pg_port=free_port(), app_port=free_port())
    (work / 'marker').write_text(ctx['marker'])
    dump(work / 'context.json', ctx)
    (PRIVATE / 'latest.txt').write_text(str(work / 'context.json'))
    inventory = []
    for folder, path in files():
        copied = work / 'originals' / str(folder) / path.name
        copied.parent.mkdir(parents=True, exist_ok=True)
        copied.write_bytes(path.read_bytes())
        assert sha(copied) == sha(path)
        book = openpyxl.load_workbook(copied, read_only=True, data_only=True)
        inventory.append(dict(folder=folder, source=str(path), copy=str(copied), sha256=sha(path),
            bytes=path.stat().st_size, sheets=[dict(name=s.title, rows=s.max_row, columns=s.max_column) for s in book]))
        book.close()
    dump(work / 'inventory.json', inventory)
    # Only these working-app endpoints are used here; never export raw client data.
    with httpx.Client(base_url='http://127.0.0.1:5000', timeout=60) as live:
        live.post('/api/auth/login', json=working_credentials()).raise_for_status()
        response = live.get('/api/templates'); response.raise_for_status()
        templates = [t for t in response.json() if t['template_name'] in {s[0] for s in SPEC.values()}]
        assert len(templates) == 6
        saved = {}
        for t in templates:
            response = live.get('/api/upload/saved-mapping', params=dict(client_id=5, template_id=t['id']))
            response.raise_for_status(); saved[t['template_name']] = response.json()
    dump(work / 'working-template-config.json', templates)
    dump(work / 'working-saved-mappings.json', saved)
    tool(work, 'initdb', ['-D', work/'cluster', '-U', 'upload_perf', '-A', 'trust', '--encoding=UTF8', '--no-locale'])
    tool(work, 'pg_ctl', ['-D', work/'cluster', '-l', work/'postgres.log', '-o',
        f"-h 127.0.0.1 -p {ctx['pg_port']}", '-w', 'start'])
    admin = psycopg2.connect(host='127.0.0.1', port=ctx['pg_port'], user='upload_perf', dbname='postgres')
    try:
        admin.autocommit = True
        with admin.cursor() as cur:
            cur.execute('CREATE DATABASE upload_perf_review')
    finally:
        admin.close()
    database = isolated_environment(ctx)
    with connection(ctx) as conn:
        database.init_db_postgres(conn)
    database.init_db()
    seed_and_start(ctx, templates, saved)


def seed_and_start(ctx, templates, saved):
    work = Path(ctx['work'])
    import bcrypt
    ctx['client_id'] = write(ctx, "INSERT INTO clients(client_code,client_name) VALUES ('PERF-REVIEW','Performance Review — isolated') ON CONFLICT(client_code) DO UPDATE SET client_name=EXCLUDED.client_name RETURNING id")
    write(ctx, 'INSERT INTO users(username,password_hash,role,is_active) VALUES (%s,%s,%s,TRUE) ON CONFLICT(username) DO UPDATE SET password_hash=EXCLUDED.password_hash',
        ('codex', bcrypt.hashpw(b'codex', bcrypt.gensalt()).decode(), 'admin'))
    write(ctx, "INSERT INTO app_settings(key,value) VALUES ('development_allow_unbacked_uploads','true') ON CONFLICT(key) DO UPDATE SET value='true'")
    ctx['templates'] = {}
    for t in templates:
        tid = write(ctx, 'INSERT INTO templates(template_name,rate_source,data_purpose) VALUES (%s,%s,%s) ON CONFLICT(template_name) DO UPDATE SET rate_source=EXCLUDED.rate_source,data_purpose=EXCLUDED.data_purpose RETURNING id',
            (t['template_name'], t.get('rate_source'), t.get('data_purpose', 'standard')))
        col_ids = {}
        for pos, col in enumerate(t['columns']):
            cid = write(ctx, 'INSERT INTO master_columns(column_name,data_type) VALUES (%s,%s) ON CONFLICT(column_name) DO UPDATE SET data_type=EXCLUDED.data_type RETURNING id',
                (col['column_name'], col['data_type']))
            col_ids[col['id']] = cid
            write(ctx, 'INSERT INTO template_column_mappings(template_id,column_id,sort_order,reconciliation_role,compare_for_changes,comparison_review_required) VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT(template_id,column_id) DO UPDATE SET sort_order=EXCLUDED.sort_order,reconciliation_role=EXCLUDED.reconciliation_role,compare_for_changes=EXCLUDED.compare_for_changes,comparison_review_required=EXCLUDED.comparison_review_required',
                (tid, cid, pos, col.get('reconciliation_role'), col.get('compare_for_changes'), col.get('comparison_review_required', False)))
        ctx['templates'][t['template_name']] = dict(id=tid, column_ids=col_ids,
            missing_document_date=not any(c.get('reconciliation_role') == 'document_date' for c in t['columns']))
        # Saved mappings retain only headers present in each workbook (same UI rule).
        for mapping in saved[t['template_name']].get('mappings', []):
            cid = col_ids.get(mapping['template_column_id'])
            if cid:
                write(ctx, 'INSERT INTO saved_column_mappings(client_id,template_id,template_column_id,excel_column) VALUES (%s,%s,%s,%s) ON CONFLICT(client_id,template_id,template_column_id) DO UPDATE SET excel_column=EXCLUDED.excel_column',
                    (ctx['client_id'], tid, cid, mapping['excel_column']))
    dump(work/'context.json', ctx)
    log = (work/'app.log').open('wb')
    process = subprocess.Popen([str(ROOT/'venv/Scripts/python.exe'), str(Path(__file__).resolve()), '--serve'],
        cwd=ROOT, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    ctx['app_pid'] = process.pid
    dump(work/'context.json', ctx)
    for _ in range(90):
        time.sleep(1)
        try:
            health = httpx.get(f"http://127.0.0.1:{ctx['app_port']}/healthz", timeout=2).json()
            if health.get('postgres') == 'ready':
                print(json.dumps(dict(status='ready',app_port=ctx['app_port'],pg_port=ctx['pg_port'],work=str(work),files=len(json.loads((work/'inventory.json').read_text())))),flush=True)
                return
        except (httpx.HTTPError, ValueError):
            pass
    raise RuntimeError('Isolated server did not become ready; inspect private app.log')


def serve():
    ctx = load_context()
    isolated_environment(ctx)
    # A real connection check is mandatory before full app startup and migrations.
    query(ctx, 'SELECT 1')
    import main
    import uvicorn
    uvicorn.run(main.app, host='127.0.0.1', port=ctx['app_port'], log_level='warning')


class Review:
    def __init__(self, ctx):
        self.ctx, self.work = ctx, Path(ctx['work'])
        self.path = self.work/'results.json'
        self.results = json.loads(self.path.read_text()) if self.path.exists() else []
        self.http = httpx.Client(base_url=f"http://127.0.0.1:{ctx['app_port']}", timeout=600)
        self.http.post('/api/auth/login', json=dict(username='codex',password='codex')).raise_for_status()
        self.http.put('/api/auth/select-client', json=dict(client_id=ctx['client_id'])).raise_for_status()

    def record(self, name, action):
        start = time.perf_counter()
        try:
            detail = action()
            result = dict(name=name,status='PASS',detail=detail)
        except Exception as exc:
            result = dict(name=name,status='FAIL',error=str(exc),traceback=traceback.format_exc())
        result['seconds'] = round(time.perf_counter()-start,4)
        result['code_phase'] = self.ctx.get('code_phase','before')
        self.results.append(result); dump(self.path,self.results)
        print(json.dumps({k:v for k,v in result.items() if k not in ('detail','traceback')},default=str),flush=True)
        return result

    def request(self, method, path, **kwargs):
        start=time.perf_counter()
        response=self.http.request(method,path,**kwargs)
        if response.is_error:
            raise AssertionError(f'{method} {path}: HTTP {response.status_code}: {response.text[:1800]}')
        return response.json(),dict(ms=round((time.perf_counter()-start)*1000,2),bytes=len(response.content))

    def source(self, folder, size):
        name=SPEC[folder][1 if size=='small' else 2]
        return next(Path(i['copy']) for i in json.loads((self.work/'inventory.json').read_text()) if i['folder']==folder and Path(i['copy']).name==name)

    def stage(self, folder, path, client=None, mode='replace_range'):
        tid=self.ctx['templates'][SPEC[folder][0]]['id']
        staged, upload_ms=self.request('POST','/api/import-excel',files={'file':(path.name,path.read_bytes(),'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')})
        book=openpyxl.load_workbook(path,read_only=True,data_only=True)
        sheet=book.worksheets[0]; data=list(sheet.values); sheet_name=sheet.title; book.close()
        headers=list(data[0]); rows=data[1:]
        mapped,map_ms=self.request('POST','/api/upload/auto-map',json=dict(file_id=staged['file_id'],template_id=tid,sheet_name=sheet_name))
        saved,_=self.request('GET','/api/upload/saved-mapping',params=dict(client_id=self.ctx['client_id'],template_id=tid))
        mappings=mapped['mappings']
        for m in mappings:
            saved_col=next((s['excel_column'] for s in saved.get('mappings',[]) if s['template_column_id']==m['column_id'] and s['excel_column'] in headers),None)
            if saved_col: m['excel_column']=saved_col
        key=next(m for m in mappings if m.get('reconciliation_role')=='document_id')
        if not key.get('excel_column') or key['excel_column'] not in headers:
            raise AssertionError(f"Required manual mapping: {key['column_name']}; available headers: {headers}")
        date_col=next((m for m in mappings if m.get('reconciliation_role')=='document_date'),None)
        if date_col is None:
            date_col=next(m for m in mappings if m['data_type']=='Date' and m['excel_column'])
        key_index=headers.index(key['excel_column']); date_index=headers.index(date_col['excel_column'])
        text_dates=[str(r[date_index]) for r in rows if r[date_index] is not None and not isinstance(r[date_index],(datetime,date,int,float))]
        delimiters={d for value in text_dates for d in ('/','-') if re.match(r'^\d{1,4}'+re.escape(d)+r'\d{1,2}'+re.escape(d)+r'\d{1,4}$',value.strip())}
        delimiter=next(iter(delimiters)) if len(delimiters)==1 else '/'
        def day(value):
            if isinstance(value,datetime):return value.date().isoformat()
            if isinstance(value,date):return value.isoformat()
            return datetime.strptime(str(value).strip(),'%d'+delimiter+'%m'+delimiter+'%Y').date().isoformat()
        detail=[]; excluded=[]; totals=[]
        for number,row in enumerate(rows,2):
            k=str(row[key_index] or '').strip()
            if not any(v is not None for v in row): excluded.append(number);continue
            if not k or re.fullmatch(r'(grand\s*)?total\s*:?',k,re.I):
                if any(isinstance(v,str) and re.fullmatch(r'(grand\s*)?total\s*:?',v.strip(),re.I) for v in row):totals.append(number)
                excluded.append(number);continue
            detail.append((number,row,day(row[date_index])))
        assert detail,'No independently recognised transaction rows'
        dates=[d for _,_,d in detail]
        footings=[m for m in mappings if m.get('reconciliation_role')=='footing']
        assert len(totals)<=1,dict(total_candidates=totals)
        payload=dict(file_id=staged['file_id'],client_id=client or self.ctx['client_id'],template_id=tid,sheet_name=sheet_name,
            comparison_mode=mode,coverage_start=min(dates),coverage_end=max(dates),confirm_report_coverage=True,
            date_delimiter=delimiter,date_order='DMY',control_total_row=totals[0] if totals and footings else None,
            control_total_confirmed=bool(totals and footings),excluded_rows=[n for n in excluded if n not in totals],
            mappings=[dict(template_column_id=m['column_id'],template_column_name=m['column_name'],excel_column_name=m['excel_column']) for m in mappings])
        expected=[]
        for _,row,day_value in detail:
            raw={m['column_name']:row[headers.index(m['excel_column'])] if m['excel_column'] else None for m in mappings}
            expected.append(dict(raw=raw,day=day_value))
        return dict(payload=payload,mappings=mappings,headers=headers,expected=expected,total_row=totals[0] if totals else None,
            file=str(path),stages=dict(upload=upload_ms,mapping=map_ms),rows=len(detail),dates=[min(dates),max(dates)])

    def validate(self, staged):
        result,timing=self.request('POST','/api/upload/validate?perf=true',json=staged['payload'])
        staged['stages']['validation']=timing
        return result

    def validate_with_acknowledgement(self, staged):
        result=self.validate(staged)
        if result.get('footing_variance_confirmation_required'):
            checks=result['source_reconciliation']['checks']
            assert all(abs(Decimal(c['difference']))<=Decimal('1') for c in checks)
            staged['acknowledged_variance']=checks
            staged['stages']['validation_before_acknowledgement']=staged['stages'].pop('validation')
            staged['payload']['confirm_footing_variance']=True
            result=self.validate(staged)
        return result

    def apply(self, staged, result):
        assert result.get('can_import'),json.dumps(result,default=str)[:1600]
        payload={**staged['payload'],'validation_plan_id':result['validation_plan_id']}
        result,timing=self.request('POST','/api/upload/import?perf=true',json=payload)
        assert result['status']=='success',json.dumps(result,default=str)[:1000]
        staged['stages']['import']=timing
        return result

    def stored(self, staged):
        p=staged['payload']
        return query(self.ctx,'SELECT id,row_data,extract_date,source_identity FROM client_data WHERE client_id=%s AND template_id=%s AND is_deleted=FALSE ORDER BY id',(p['client_id'],p['template_id']))

    def verify(self, staged):
        actual=self.stored(staged)
        assert len(actual)==staged['rows'],dict(expected=staged['rows'],stored=len(actual))
        assert len({r['source_identity'] for r in actual})==len(actual),'Duplicate active source identities'
        key=next(m['column_name'] for m in staged['mappings'] if m.get('reconciliation_role')=='document_id')
        assert Counter((str(r['raw'][key]),r['day']) for r in staged['expected'])==Counter((str(r['row_data'][key]),str(r['extract_date'])) for r in actual)
        def normalized(value,kind):
            if value is None or value=='':return ('blank',)
            if isinstance(value,(datetime,date)):return ('date',value.date().isoformat() if isinstance(value,datetime) else value.isoformat())
            if kind=='Date' and isinstance(value,str):
                for fmt in ('%Y-%m-%d','%Y-%m-%dT%H:%M:%S','%d/%m/%Y','%d-%m-%Y'):
                    try:return ('date',datetime.strptime(value,fmt).date().isoformat())
                    except ValueError:pass
            if kind=='Numeric':return ('number',str(Decimal(str(value).replace(',','')).normalize()))
            return ('raw',str(value))
        def signature(raw):
            return tuple(normalized(raw.get(m['column_name']),m['data_type']) for m in staged['mappings'])
        expected_values=Counter(signature(r['raw']) for r in staged['expected'])
        stored_values=Counter(signature(r['row_data']) for r in actual)
        assert expected_values==stored_values,dict(missing_occurrences=sum((expected_values-stored_values).values()),extra_occurrences=sum((stored_values-expected_values).values()))
        totals={}
        for m in staged['mappings']:
            if m.get('reconciliation_role')!='footing':continue
            name=m['column_name']
            def amount(v):return Decimal(str(v or 0).replace(',',''))
            excel=sum((amount(r['raw'][name]) for r in staged['expected']),Decimal(0))
            stored=sum((amount(r['row_data'].get(name)) for r in actual),Decimal(0))
            assert excel==stored,dict(column=name,excel=str(excel),stored=str(stored))
            totals[name]=str(excel)
        return dict(rows=len(actual),documents=len({r['row_data'][key] for r in actual}),totals=totals)

    def databank(self, staged):
        p=staged['payload']; samples=[]
        for page in [1,2,1]:
            data,timing=self.request('GET','/api/data/preview',params=dict(client_id=p['client_id'],template_id=p['template_id'],
                date_start=p['coverage_start'],date_end=p['coverage_end'],page=page,limit=50,include_metrics='true',perf='true'))
            assert data['total_rows']==staged['rows'],dict(total=data['total_rows'],expected=staged['rows'])
            assert data['preview_metrics']['row_count']==staged['rows']
            samples.append(dict(page=page,**timing))
        return samples


def baseline(ctx, only=None):
    review=Review(ctx)
    for folder in (only or list(SPEC)):
        for size in ['small','large']:
            def action(folder=folder,size=size):
                cid=write(ctx,'INSERT INTO clients(client_code,client_name) VALUES (%s,%s) RETURNING id',
                    ('PERF-'+uuid.uuid4().hex,'Performance baseline '+str(folder)+' '+size))
                staged=review.stage(folder,review.source(folder,size),client=cid)
                dump(review.work/f'baseline-{folder}-{size}.json',dict(staged=staged))
                v=review.validate_with_acknowledgement(staged)
                dump(review.work/f'baseline-{folder}-{size}.json',dict(staged=staged,validation=v))
                if v.get('can_import'):
                    result=review.apply(staged,v)
                    proof=review.verify(staged)
                    unchanged=[]
                    for trial in range(3):
                        repeated=review.stage(folder,review.source(folder,size),client=cid)
                        before=review.stored(repeated)
                        vv=review.validate_with_acknowledgement(repeated)
                        assert not vv.get('can_import') and not vv.get('has_errors'),json.dumps(vv,default=str)[:1000]
                        assert before==review.stored(repeated),'Unchanged validation wrote canonical rows'
                        unchanged.append(repeated['stages'])
                    return dict(stages=staged['stages'],proof=proof,unchanged=unchanged,databank=review.databank(staged),import_result=result)
                raise AssertionError(json.dumps(dict(stages=staged['stages'],validation=v),default=str)[:3000])
            label='configured fixture' if ctx.get('fixture_adjustments') else 'original baseline'
            review.record(f'{folder} {SPEC[folder][0]} {size}: {label}',action)


def configure_fixtures(ctx):
    """Supply explicitly labelled test-only roles after recording original blockers."""
    review=Review(ctx)
    templates=json.loads((review.work/'working-template-config.json').read_text())
    changes=[]
    for template in templates:
        info=ctx['templates'][template['template_name']]
        if not info['missing_document_date']:continue
        ids={int(k):v for k,v in info['column_ids'].items()}
        roles={ids[c['id']]:c.get('reconciliation_role') for c in template['columns']}
        selected=next(c for c in template['columns'] if c['column_name']=='Transaction Date')
        roles[ids[selected['id']]]='document_date'
        review.request('PUT',f"/api/templates/{info['id']}",json=dict(template_name=template['template_name'],
            selected_columns=[ids[c['id']] for c in template['columns']],column_roles=roles,
            rate_source=template.get('rate_source'),has_subtotals_by_default=template.get('has_subtotals_by_default',False)))
        changes.append(dict(template=template['template_name'],column=selected['column_name'],test_only=True))
        if template['template_name'] in ('Gate Pass','Vendor Bill'):
            for col in template['columns']:
                source={'GP no':'GP #','Bill no':'Bill #','Transaction Date':'Date','Quantity':'Qty','Receive Quantity':'Qty','Amount':'Total'}.get(col['column_name'])
                if source:
                    write(ctx,'INSERT INTO saved_column_mappings(client_id,template_id,template_column_id,excel_column) VALUES (%s,%s,%s,%s) ON CONFLICT(client_id,template_id,template_column_id) DO UPDATE SET excel_column=EXCLUDED.excel_column',
                        (ctx['client_id'],info['id'],ids[col['id']],source))
                    changes.append(dict(template=template['template_name'],column=col['column_name'],source=source,test_only=True))
    ctx['fixture_adjustments']=changes
    dump(review.work/'context.json',ctx)
    print(json.dumps(dict(test_only_adjustments=changes)),flush=True)


def variant(review, staged, name, edit):
    book=openpyxl.load_workbook(staged['file'],data_only=True)
    sheet=book.worksheets[0]
    edit(sheet)
    # Freeze original cached results in copies; Excel is never asked to recalculate
    # or save the originals. Recompute only controls affected by the chosen edit.
    footer=sheet.max_row
    key=next(m for m in staged['mappings'] if m.get('reconciliation_role')=='document_id')
    ki=staged['headers'].index(key['excel_column'])+1
    if any(re.fullmatch(r'(grand\s*)?total\s*:?',str(cell.value or '').strip(),re.I) for cell in sheet[footer]) or footer==staged.get('total_row'):
        for m in staged['mappings']:
            if m.get('reconciliation_role')=='footing' and m['excel_column']:
                ci=staged['headers'].index(m['excel_column'])+1
                total=sum((Decimal(str(sheet.cell(r,ci).value or 0)) for r in range(2,footer)),Decimal(0))
                sheet.cell(footer,ci,float(total))
    output=review.work/'variants'/f'{name}-{uuid.uuid4().hex[:6]}.xlsx'
    output.parent.mkdir(exist_ok=True)
    book.save(output);book.close()
    return output


def mutations(ctx, only=None):
    review=Review(ctx)
    for folder in (only or list(SPEC)):
        def action(folder=folder):
            cid=write(ctx,'INSERT INTO clients(client_code,client_name) VALUES (%s,%s) RETURNING id',
                ('PERF-'+uuid.uuid4().hex,'Mutation fixture '+str(folder)))
            base=review.stage(folder,review.source(folder,'small'),client=cid)
            v=review.validate(base);review.apply(base,v);proof=review.verify(base)
            review.databank(base)  # Prime scoped reads before correction/deletion.
            before=review.stored(base)
            # Identical file with a different name must remain a no-op.
            renamed=review.work/'variants'/('renamed-'+Path(base['file']).name)
            renamed.parent.mkdir(exist_ok=True);renamed.write_bytes(Path(base['file']).read_bytes())
            same=review.stage(folder,renamed,client=cid)
            vv=review.validate(same)
            assert not vv.get('can_import') and not vv.get('has_errors'),vv
            assert before==review.stored(same)
            # A second delivery of the original approved import is a receipt retry.
            retry=review.apply(base,v)
            assert before==review.stored(base)
            def reorder(sheet):
                end=base['total_row']-1 if base['total_row'] else sheet.max_row
                values=list(sheet.iter_rows(min_row=2,max_row=end,values_only=True))[::-1]
                for row_number,values_row in enumerate(values,2):
                    for col_number,value in enumerate(values_row,1):sheet.cell(row_number,col_number).value=value
            reordered=review.stage(folder,variant(review,base,'reordered',reorder),client=cid)
            vv=review.validate(reordered)
            assert not vv.get('has_errors'),json.dumps(vv,default=str)[:1000]
            changes=vv.get('change_detection',{})
            assert all(not changes.get(k,0) for k in ['new_count','changed_count','missing_count']),changes
            # Source-order metadata may refresh without changing accounting rows.
            if vv.get('can_import'):review.apply(reordered,vv)
            assert before==review.stored(base)
            # Change one genuine amount/quantity; dependent control totals move with it.
            value_col=next((m for m in base['mappings'] if m.get('reconciliation_role')=='footing'),
                next(m for m in base['mappings'] if m['data_type']=='Numeric' and m['excel_column']))
            column=base['headers'].index(value_col['excel_column'])+1
            def change(sheet):sheet.cell(2,column,float(Decimal(str(sheet.cell(2,column).value or 0))+Decimal('1')))
            changed=review.stage(folder,variant(review,base,'changed',change),client=cid)
            vv=review.validate(changed);result=review.apply(changed,vv);review.verify(changed)
            assert result.get('updated_count')==1,dict(result=result)
            # Delete one detail line while retaining the original full report period.
            shorter=review.stage(folder,variant(review,changed,'deleted',lambda sheet:sheet.delete_rows(2,1)),client=cid)
            shorter['payload'].update(coverage_start=base['payload']['coverage_start'],coverage_end=base['payload']['coverage_end'])
            vv=review.validate(shorter);deleted=review.apply(shorter,vv);review.verify(shorter)
            assert deleted.get('deleted_count')==1,deleted
            before=review.stored(shorter)
            partial=review.stage(folder,variant(review,shorter,'partial',lambda sheet:sheet.delete_rows(2,1)),client=cid,mode='correction')
            vv=review.validate(partial)
            assert not vv.get('has_errors'),json.dumps(vv,default=str)[:1000]
            assert all(not vv.get('change_detection',{}).get(k,0) for k in ['new_count','changed_count','missing_count'])
            if vv.get('can_import'):review.apply(partial,vv)
            assert before==review.stored(shorter),'Partial extract removed absent transactions'
            return dict(initial=proof,renamed_noop=True,reordered_noop=True,receipt_retry=retry.get('status'),
                corrected_lines=result.get('updated_count'),deleted_lines=deleted.get('deleted_count'),partial_preserves_absent=True,
                databank=review.databank(shorter))
        review.record(f'{folder} {SPEC[folder][0]}: controlled mutations',action)


def profile_validation(ctx):
    import cProfile,pstats
    isolated_environment(ctx)
    from routers import import_export as upload
    work=Path(ctx['work']);staged=json.loads((work/'baseline-2-large.json').read_text())['staged']
    profile=cProfile.Profile();started=time.perf_counter();phase=ctx.get('code_phase','before')
    profile.enable()
    try:result=upload.validate_only(upload.ImportRequest(**staged['payload']),perf=True)
    finally:
        profile.disable();profile.dump_stats(str(work/f'po-large-validation-{phase}.prof'))
        with (work/f'po-large-validation-{phase}-profile.txt').open('w',encoding='utf-8') as out:
            pstats.Stats(profile,stream=out).sort_stats('cumulative').print_stats(35)
    print(json.dumps(dict(seconds=round(time.perf_counter()-started,2),has_errors=result.get('has_errors'),can_import=result.get('can_import'))),flush=True)


def extended_cases(ctx):
    review=Review(ctx)
    def new_client(label):
        return write(ctx,'INSERT INTO clients(client_code,client_name) VALUES (%s,%s) RETURNING id',
            ('PERF-'+uuid.uuid4().hex,'Extended fixture '+label))
    def duplicate_and_empty():
        cid=new_client('duplicates-empty')
        base=review.stage(2,review.source(2,'small'),client=cid)
        review.apply(base,review.validate(base));original=review.stored(base)
        def add_copy(sheet):
            values=[c.value for c in sheet[2]]
            footer=base['total_row'];sheet.insert_rows(footer)
            for column,value in enumerate(values,1):sheet.cell(footer,column).value=value
        duplicated=review.stage(2,variant(review,base,'legitimate-repeated-line',add_copy),client=cid)
        result=review.apply(duplicated,review.validate(duplicated));proof=review.verify(duplicated)
        assert proof['rows']==base['rows']+1 and result['imported_count']==1,result
        assert {r['id'] for r in original}.issubset({r['id'] for r in review.stored(duplicated)})
        same=review.stage(2,Path(duplicated['file']),client=cid);vv=review.validate(same)
        assert not vv['can_import'] and not vv['has_errors']
        key=next(m for m in base['mappings'] if m.get('reconciliation_role')=='document_id')
        column=base['headers'].index(key['excel_column'])+1
        def remove_voucher(sheet):
            document=sheet.cell(2,column).value
            for number in range(sheet.max_row-1,1,-1):
                if sheet.cell(number,column).value==document:sheet.delete_rows(number)
        removed=review.stage(2,variant(review,duplicated,'whole-voucher-removed',remove_voucher),client=cid)
        removed['payload'].update(coverage_start=base['dates'][0],coverage_end=base['dates'][1])
        result=review.apply(removed,review.validate(removed));review.verify(removed)
        assert result['deleted_count']==duplicated['rows']-removed['rows']
        def empty(sheet):
            sheet.delete_rows(2,sheet.max_row)
            controls=[None]*len(base['headers']);controls[column-1]='TOTAL'
            for m in base['mappings']:
                if m.get('reconciliation_role')=='footing':controls[base['headers'].index(m['excel_column'])]=0
            sheet.append(controls)
        empty_path=variant(review,base,'empty-report',empty)
        staged,_=review.request('POST','/api/import-excel',files={'file':(empty_path.name,empty_path.read_bytes(),'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')})
        empty_payload={**base['payload'],'file_id':staged['file_id'],'control_total_row':2,'excluded_rows':[]}
        response=review.http.post('/api/upload/validate',json=empty_payload)
        assert response.status_code==400 and 'empty' in response.text.casefold(),response.text
        empty_payload['confirm_empty_period']=True
        empty_stage={**base,'payload':empty_payload,'rows':0,'expected':[],'stages':{}}
        result=review.apply(empty_stage,review.validate(empty_stage))
        assert result['deleted_count']==removed['rows'] and not review.stored(empty_stage),result
        return dict(legitimate_extra_line_preserved=True,repeat_noop=True,whole_voucher_removed=True,
            empty_confirmation_required=True,empty_removed=result['deleted_count'])
    review.record('PO repeated lines, whole-voucher deletion and confirmed empty report',duplicate_and_empty)
    def overlap():
        cid=new_client('overlap')
        inventory=json.loads((review.work/'inventory.json').read_text())
        def path(name):return next(Path(i['copy']) for i in inventory if Path(i['copy']).name==name)
        for name in ['PO 11.xlsx','PO 12-02.xlsx','PO 11 to 14 .xlsx']:
            staged=review.stage(2,path(name),client=cid)
            result=review.validate(staged)
            if result.get('can_import'):review.apply(staged,result)
        proof=review.verify(staged)
        outside=query(ctx,'SELECT id,row_data,version FROM client_data WHERE client_id=%s AND template_id=%s AND extract_date<>%s::date ORDER BY id',
            (cid,staged['payload']['template_id'],'2026-02-11'))
        daily=review.stage(2,path('PO 11.xlsx'),client=cid);v=review.validate(daily)
        if v.get('can_import'):review.apply(daily,v)
        assert outside==query(ctx,'SELECT id,row_data,version FROM client_data WHERE client_id=%s AND template_id=%s AND extract_date<>%s::date ORDER BY id',
            (cid,staged['payload']['template_id'],'2026-02-11'))
        return dict(combined_report=proof,outside_daily_coverage_preserved=len(outside))
    review.record('PO daily and overlapping reports preserve outside coverage',overlap)
    def concurrent_delivery():
        cid=new_client('concurrent-delivery')
        staged=review.stage(2,review.source(2,'small'),client=cid)
        result=review.validate(staged)
        payload={**staged['payload'],'validation_plan_id':result['validation_plan_id']}
        with ThreadPoolExecutor(max_workers=2) as executor:
            responses=list(executor.map(lambda _:review.http.post('/api/upload/import',json=payload),range(2)))
        assert any(r.status_code==200 for r in responses)
        assert all(r.status_code in (200,409) for r in responses),[(r.status_code,r.text[:500]) for r in responses]
        proof=review.verify(staged)
        return dict(http_statuses=[r.status_code for r in responses],proof=proof)
    review.record('PO simultaneous delivery preserves one set of transaction lines',concurrent_delivery)


def browser_checks(ctx, only=None):
    from playwright.sync_api import sync_playwright
    review=Review(ctx)
    with sync_playwright() as pw:
        browser=pw.chromium.launch(headless=True)
        try:
            for folder in (only or list(SPEC)):
                for size in ['small','large']:
                    context=browser.new_context(viewport=dict(width=1366,height=900))
                    url=f"http://127.0.0.1:{ctx['app_port']}"
                    # Even a hardcoded product URL must never escape this test app.
                    context.route('**/*',lambda route:route.continue_() if route.request.url.startswith(url+'/') else route.abort())
                    page=context.new_page();page.set_default_timeout(120000)
                    page.add_init_script("window.reviewLongTasks=[]; new PerformanceObserver(list=>window.reviewLongTasks.push(...list.getEntries().map(e=>e.duration))).observe({type:'longtask',buffered:true});")
                    try:
                        page.goto(url+'/static/login.html')
                        page.get_by_role('textbox',name='Username',exact=True).fill('codex')
                        page.get_by_role('textbox',name='Password',exact=True).fill('codex')
                        page.get_by_role('button',name='Log In',exact=True).click()
                        page.get_by_role('heading',name='Performance Review — isolated',exact=True).click()
                        page.get_by_role('heading',name='Upload Client Data',exact=True).click()
                        tid=ctx['templates'][SPEC[folder][0]]['id']
                        page.locator(f'#templateDropdown option[value="{tid}"]').wait_for(state='attached')
                        for trial in range(3):
                            def action(trial=trial):
                                if trial:page.locator('#resetBtn').click()
                                page.evaluate('performance.clearResourceTimings();window.reviewLongTasks=[]')
                                start=time.perf_counter();page.locator('#templateDropdown').select_option(str(tid))
                                page.get_by_role('button',name='Upload File',exact=True).wait_for(state='visible')
                                template_ms=round((time.perf_counter()-start)*1000,2)
                                start=time.perf_counter()
                                page.locator('#fileInput').set_input_files(str(review.source(folder,size)))
                                page.locator('#goToDataBankBtn').wait_for(state='visible')
                                loaded_ms=round((time.perf_counter()-start)*1000,2)
                                # Settle date feedback before the next trial so previous
                                # worksheet reads cannot inflate the following warm run.
                                page.wait_for_function("!document.querySelector('#documentDateSuggestion').textContent.includes('Calculating suggested dates')")
                                ready_ms=round((time.perf_counter()-start)*1000,2)
                                info=page.evaluate("()=>({requests:performance.getEntriesByType('resource').filter(e=>e.initiatorType==='fetch').map(e=>({path:new URL(e.name).pathname,ms:e.duration,transfer:e.transferSize})),longTasks:window.reviewLongTasks,heap:performance.memory?.usedJSHeapSize})")
                                if trial==0:page.screenshot(path=str(review.work/f"browser-{ctx.get('code_phase','before')}-{folder}-{size}.png"),full_page=False)
                                return dict(template_ms=template_ms,file_to_mapping_ms=loaded_ms,file_to_date_feedback_ms=ready_ms,date_feedback_waited=True,trial=trial+1,
                                    browser_state='cold page' if trial==0 else 'warm page',**info)
                            review.record(f'{folder} {SPEC[folder][0]} {size}: browser load trial {trial+1}',action)
                    finally:context.close()
        finally:browser.close()


def working_reads(ctx):
    """Read populated working scopes only; never submit a working import/validation."""
    review=Review(ctx)
    templates=json.loads((review.work/'working-template-config.json').read_text())
    with httpx.Client(base_url='http://127.0.0.1:5000',timeout=120) as live:
        live.post('/api/auth/login',json=working_credentials()).raise_for_status()
        for folder in SPEC:
            template=next(t for t in templates if t['template_name']==SPEC[folder][0])
            small_day={1:'2026-01-03',2:'2026-02-11',3:'2026-02-11',4:'2026-11-03',5:'2026-11-04',6:'2026-11-03'}[folder]
            for scope,start,end in [('small',small_day,small_day),('large','2026-01-01','2026-11-06')]:
                def action(template=template,start=start,end=end):
                    timings=[]
                    for _ in range(3):
                        started=time.perf_counter()
                        response=live.get('/api/data/preview',params=dict(client_id=5,template_id=template['id'],date_start=start,date_end=end,
                            page=1,limit=50,include_metrics='true',perf='true'))
                        response.raise_for_status();data=response.json()
                        timings.append(dict(ms=round((time.perf_counter()-started)*1000,2),bytes=len(response.content),
                            rows=data.get('total_rows'),metrics_status=data.get('preview_metrics',{}).get('status')))
                    return dict(environment='working app; read-only preview GETs',timings=timings)
                review.record(f'{folder} {SPEC[folder][0]} {scope}: working Data Bank reads',action)


def confirmed_date_checks(ctx):
    review=Review(ctx)
    for folder in SPEC:
        for size in ['small','large']:
            def action(folder=folder,size=size):
                staged=json.loads((review.work/f'baseline-{folder}-{size}.json').read_text())['staged']
                p=staged['payload']
                mapped=[dict(template_column_name=m['column_name'],excel_column_name=m['excel_column'])
                    for m in staged['mappings'] if m.get('excel_column')]
                dates={m['column_name'] for m in staged['mappings'] if m['data_type']=='Date'}
                payload={key:p[key] for key in ['file_id','template_id','sheet_name','date_delimiter','date_order',
                    'control_total_row','control_total_confirmed']}
                payload.update(column_mappings=mapped,date_mappings=[m for m in mapped if m['template_column_name'] in dates])
                result,timing=review.request('POST','/api/upload/date-preview',json=payload)
                suggestion=result['date_suggestion']
                assert [suggestion['start'],suggestion['end']]==staged['dates'],suggestion
                assert suggestion['transaction_dates']==staged['rows'],suggestion
                return dict(timing=timing,range=staged['dates'],valid_dates=suggestion['transaction_dates'])
            review.record(f'{folder} {SPEC[folder][0]} {size}: confirmed full-sheet date range',action)


def stop_app(ctx):
    # Identify the exact process created by this runner before terminating it.
    pid=int(ctx['app_pid'])
    command=subprocess.check_output(['powershell','-NoProfile','-Command',
        f'(Get-CimInstance Win32_Process -Filter "ProcessId = {pid}").CommandLine'],text=True,
        creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0)).strip()
    assert str(Path(__file__).resolve()).casefold() in command.casefold() and '--serve' in command,command
    query(ctx,'SELECT 1')
    os.kill(pid,signal.SIGTERM)
    time.sleep(1)


def restart(ctx):
    stop_app(ctx)
    log=(Path(ctx['work'])/'app-after.log').open('wb')
    process=subprocess.Popen([str(ROOT/'venv/Scripts/python.exe'),str(Path(__file__).resolve()),'--serve'],
        cwd=ROOT,stdout=log,stderr=subprocess.STDOUT,stdin=subprocess.DEVNULL,
        creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    ctx.update(app_pid=process.pid,code_phase='after')
    dump(Path(ctx['work'])/'context.json',ctx)
    for _ in range(60):
        time.sleep(1)
        try:
            if httpx.get(f"http://127.0.0.1:{ctx['app_port']}/healthz",timeout=2).json().get('postgres')=='ready':
                print('Isolated app restarted with repaired code',flush=True);return
        except (httpx.HTTPError,ValueError):pass
    raise RuntimeError('Isolated repaired server did not become ready')


def original_integrity(ctx):
    inventory=json.loads((Path(ctx['work'])/'inventory.json').read_text())
    changed=[i['source'] for i in inventory if sha(Path(i['source']))!=i['sha256']]
    assert not changed,changed
    print(json.dumps(dict(originals_unchanged=len(inventory))),flush=True)


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--setup',action='store_true');parser.add_argument('--serve',action='store_true')
    parser.add_argument('--resume-setup',action='store_true')
    parser.add_argument('--baseline',action='store_true');parser.add_argument('--integrity',action='store_true')
    parser.add_argument('--configure-fixtures',action='store_true');parser.add_argument('--mutations',action='store_true')
    parser.add_argument('--browser',action='store_true');parser.add_argument('--profile',action='store_true')
    parser.add_argument('--working-reads',action='store_true')
    parser.add_argument('--restart',action='store_true');parser.add_argument('--stop',action='store_true')
    parser.add_argument('--extended',action='store_true')
    parser.add_argument('--date-checks',action='store_true')
    parser.add_argument('--folder',type=int,choices=list(SPEC),action='append')
    args=parser.parse_args()
    if args.setup:setup();return
    if args.serve:serve();return
    ctx=load_context()
    if args.resume_setup:
        isolated_environment(ctx)
        work=Path(ctx['work'])
        seed_and_start(ctx,json.loads((work/'working-template-config.json').read_text()),json.loads((work/'working-saved-mappings.json').read_text()))
        return
    if args.baseline:baseline(ctx,args.folder)
    if args.configure_fixtures:configure_fixtures(ctx)
    if args.mutations:mutations(ctx,args.folder)
    if args.browser:browser_checks(ctx,args.folder)
    if args.profile:profile_validation(ctx)
    if args.extended:extended_cases(ctx)
    if args.working_reads:working_reads(ctx)
    if args.date_checks:confirmed_date_checks(ctx)
    if args.restart:restart(ctx)
    if args.stop:
        original_integrity(ctx);stop_app(ctx)
        tool(Path(ctx['work']),'pg_ctl',['-D',Path(ctx['work'])/'cluster','-m','fast','-w','stop'])
        print('Isolated app and PostgreSQL stopped; private evidence retained',flush=True)
    if args.integrity:original_integrity(ctx)


if __name__=='__main__':
    if hasattr(sys.stdout,'reconfigure'):sys.stdout.reconfigure(encoding='utf-8')
    main()
