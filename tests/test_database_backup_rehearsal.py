"""Recovery proof tests. Production pools, source exports and Drive are forbidden."""
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import tempfile
from types import SimpleNamespace
import zipfile

import pytest

from scripts import rehearse_database_backup as rehearsal


@pytest.fixture(autouse=True)
def forbid_live(monkeypatch):
    import database
    def forbidden(*args, **kwargs):
        pytest.fail('Live database pool/export is forbidden in recovery tests')
    monkeypatch.setattr(database, 'get_pg_pool', forbidden)
    monkeypatch.setattr(database, 'run_read_snapshot_pg', forbidden)
    monkeypatch.setenv('DATABASE_URL', 'postgresql://unused:never-connect@forbidden.invalid/production')
    monkeypatch.setenv('PGHOST', 'forbidden.invalid')
    monkeypatch.setenv('PGSERVICE', 'forbidden')
    monkeypatch.setenv('PGPASSWORD', 'private-production-password')
    monkeypatch.delenv('RENDER', raising=False)


def make_zip(root, content=b'PGDMPsynthetic', *, extra=None, duplicate=False):
    path = root / 'Database-fixture.zip'
    manifest = {'format':'postgresql-custom','version':1,'operation_id':'a'*32,
        'dump_file':'database.dump','dump_bytes':len(content),
        'dump_sha256':hashlib.sha256(content).hexdigest()}
    with zipfile.ZipFile(path, 'w') as z:
        z.writestr('database.dump',content)
        z.writestr('manifest.json',json.dumps(manifest))
        if extra: z.writestr(extra,b'unsafe')
        if duplicate: z.writestr('database.dump',content)
    receipt={'state':'complete','operation_id':'a'*32,'file_name':path.name,
        'bytes':path.stat().st_size,'sha256':rehearsal.backup._sha256(path)}
    (root/'receipt.json').write_text(json.dumps(receipt),encoding='utf-8')
    return path


def test_preview_does_not_touch_files_tools_or_database(tmp_path):
    root=tmp_path/'not-created'
    assert rehearsal.main(['--archive',str(root/'missing.zip'),'--tools',str(root/'tools'),
        '--work-root',str(root)])==0
    assert not root.exists()


@pytest.mark.parametrize('path', ['relative', str(rehearsal.PROJECT), str(rehearsal.PROJECT/'new-recovery')])
def test_workspace_and_relative_outputs_rejected(path):
    with pytest.raises(rehearsal.RehearsalError): rehearsal.external(path)


def test_archive_readback_preserves_every_byte_without_changing_original(tmp_path):
    path=make_zip(tmp_path); before=path.read_bytes()
    out=tmp_path/'recovery.dump'
    result=rehearsal.verified_archive(path,out)
    assert out.read_bytes()==b'PGDMPsynthetic' and path.read_bytes()==before
    assert result['zip_sha256']==hashlib.sha256(before).hexdigest()
    with pytest.raises(FileExistsError): rehearsal.verified_archive(path,out)


@pytest.mark.parametrize('defect',['receipt','content','manifest','extra','duplicate','format','magic'])
def test_modified_incomplete_or_unsafe_archives_rejected(tmp_path,defect):
    if defect=='extra': path=make_zip(tmp_path,extra='../outside')
    elif defect=='duplicate': path=make_zip(tmp_path,duplicate=True)
    elif defect=='magic': path=make_zip(tmp_path,content=b'wrong archive')
    else: path=make_zip(tmp_path)
    receipt=json.loads((tmp_path/'receipt.json').read_text())
    if defect=='receipt': receipt['state']='local_verified'
    if defect=='content': path.write_bytes(b'changed')
    if defect in ('manifest','format'):
        with zipfile.ZipFile(path) as z: m=json.loads(z.read('manifest.json'))
        m['dump_sha256' if defect=='manifest' else 'format']='bad'
        with zipfile.ZipFile(path,'w') as z:
            z.writestr('manifest.json',json.dumps(m)); z.writestr('database.dump',b'PGDMPsynthetic')
        receipt.update(bytes=path.stat().st_size,sha256=rehearsal.backup._sha256(path))
    (tmp_path/'receipt.json').write_text(json.dumps(receipt))
    with pytest.raises((rehearsal.RehearsalError,zipfile.BadZipFile)):
        rehearsal.verified_archive(path,tmp_path/'out.dump')
    assert not (tmp_path/'outside').exists()


@pytest.mark.parametrize('count',[1,3,685])
def test_row_fingerprint_preserves_legitimate_identical_line_multiplicity(count):
    h=hashlib.sha256(b'same voucher line').hexdigest()
    result=rehearsal.digest_rows(iter([(h,)]*count))
    assert result['rows']==count and result['exact_repeat_rows']==count-1
    assert result['sha256']==hashlib.sha256(bytes.fromhex(h)*count).hexdigest()


class FakeConnection:
    def __init__(self,result): self.result=result
    def cursor(self): return self
    def __enter__(self): return self
    def __exit__(self,*args): pass
    def execute(self,*args): pass
    def fetchone(self): return self.result


@pytest.mark.parametrize('defect',['ordinary','remote-server','remote-client','wrong-cluster','wrong-marker','wrong-name','socket'])
def test_guard_rejects_existing_remote_or_unowned_targets(tmp_path,defect):
    data=tmp_path/'cluster'; data.mkdir()
    (tmp_path/'cluster-marker').write_text('owned')
    cluster=SimpleNamespace(data=data,work=tmp_path,marker='owned')
    name='production' if defect=='ordinary' else 'backup_rehearsal_test'
    result=[name,'127.0.0.1/32','127.0.0.1',str(data)]
    if defect=='remote-server': result[1]='1.2.3.4'
    if defect=='remote-client': result[2]='1.2.3.4'
    if defect=='wrong-cluster': result[3]=str(tmp_path/'other')
    if defect=='wrong-marker': cluster.marker='other'
    if defect=='wrong-name': result[0]='another'
    if defect=='socket': result[1]=None
    with pytest.raises(rehearsal.RehearsalError): rehearsal.check_target(FakeConnection(result),cluster,name)


def test_guard_accepts_only_verified_loopback_cluster(tmp_path):
    data=tmp_path/'cluster'; data.mkdir(); (tmp_path/'cluster-marker').write_text('owned')
    cluster=SimpleNamespace(data=data,work=tmp_path,marker='owned')
    rehearsal.check_target(FakeConnection(['backup_rehearsal_test','127.0.0.1/32','127.0.0.1',str(data)]),cluster,'backup_rehearsal_test')


@pytest.mark.parametrize('section',['tables','metadata','sequences','accounting_totals'])
def test_mismatch_never_certifies_recovery(section):
    baseline={'tables':{'lines':{'rows':685,'sha256':'same'}},'metadata':{'grants':['SELECT']},
        'sequences':[686],'accounting_totals':['123.456789'],'accounting_amount_columns':['cashflow_debit']}
    restored=json.loads(json.dumps(baseline)); restored[section]={}
    result=rehearsal.compare(baseline,restored)
    assert not result['passed'] and result['different_sections']==[section]


def test_target_environment_never_inherits_live_connection_or_service(tmp_path):
    c=rehearsal.Cluster(None,tmp_path)
    env=c.env('backup_rehearsal_test')
    assert env['PGHOST']=='127.0.0.1' and env['PGPASSWORD']!='private-production-password'
    assert env['PGDATABASE']=='backup_rehearsal_test'
    assert 'PGSERVICE' not in env and 'DATABASE_URL' not in env


def test_native_timeout_keeps_a_precise_failure_instead_of_generic_success(tmp_path,monkeypatch):
    tool=object.__new__(rehearsal.Tools)
    tool.directory=tmp_path; tool.work=tmp_path; tool.number=0
    def timeout(*args,**kwargs):
        raise rehearsal.subprocess.TimeoutExpired('fixture tool',kwargs['timeout'])
    monkeypatch.setattr(rehearsal.subprocess,'run',timeout)
    with pytest.raises(rehearsal.RehearsalError,match='900-second limit'):
        tool.run('pg_dump',[])


def test_libpq_connection_scrubs_ambient_service_and_restores_process_environment(tmp_path,monkeypatch):
    observed=[]
    class Connection:
        def close(self): observed.append('closed')
    def connect(**kwargs):
        assert not any(k.upper().startswith('PG') for k in os.environ)
        assert kwargs['hostaddr']=='127.0.0.1' and kwargs['dbname']=='backup_rehearsal_test'
        observed.append('connected')
        return Connection()
    monkeypatch.setattr(rehearsal.psycopg2,'connect',connect)
    with rehearsal.Cluster(None,tmp_path).connection('backup_rehearsal_test'):
        assert os.environ['PGSERVICE']=='forbidden'
    assert observed==['connected','closed']


def test_server_stop_failure_cannot_leave_a_passed_recovery_receipt(tmp_path,monkeypatch):
    archive=make_zip(tmp_path)
    controls={'tables':{},'metadata':{},'sequences':[],'accounting_totals':[], 'accounting_amount_columns':[]}
    monkeypatch.setattr(rehearsal,'Tools',lambda *args:None)
    monkeypatch.setattr(rehearsal,'capture',lambda *args:(tmp_path/'fresh',controls,{}))
    class Server:
        def __init__(self,*args): pass
        def __enter__(self): return self
        def restore(self,*args): return controls
        def __exit__(self,*args): raise rehearsal.RehearsalError('Fixture server stop failed')
    monkeypatch.setattr(rehearsal,'Cluster',Server)
    with pytest.raises(rehearsal.RehearsalError): rehearsal.rehearse(archive,tmp_path,tmp_path/'work')
    result=json.loads(next((tmp_path/'work').glob('run-*/result.json')).read_text())
    assert not result['passed']


def test_compact_mode_reports_changed_source_without_new_export_or_row_repair(tmp_path,monkeypatch):
    archive=make_zip(tmp_path); original=archive.read_bytes()
    baseline={'tables':{'lines':{'rows':3,'sha256':'source'}},'metadata':{},'sequences':[],
        'accounting_totals':[],'accounting_amount_columns':[]}
    restored=json.loads(json.dumps(baseline));restored['tables']['lines']['sha256']='older-backup'
    monkeypatch.setattr(rehearsal,'Tools',lambda *args:None)
    monkeypatch.setattr(rehearsal,'snapshot_controls',lambda:(baseline,{}))
    def forbidden(*args): pytest.fail('Compact mode must not export or repair rows')
    monkeypatch.setattr(rehearsal,'capture',forbidden)
    class Server:
        def __init__(self,*args): pass
        def __enter__(self): return self
        def __exit__(self,*args): pass
        def restore(self,*args): return restored
    monkeypatch.setattr(rehearsal,'Cluster',Server)
    with pytest.raises(rehearsal.RehearsalError,match='legitimate later changes'):
        rehearsal.rehearse(archive,tmp_path,tmp_path/'work',compare_current=True)
    report=json.loads(next((tmp_path/'work').glob('run-*/result.json')).read_text())
    assert not report['passed'] and report['comparison']['different_sections']==['tables']
    assert archive.read_bytes()==original


def test_hosted_run_is_blocked_without_file_changes(tmp_path,monkeypatch):
    monkeypatch.setenv('RENDER','true')
    assert rehearsal.main(['--archive',str(tmp_path/'none'),'--tools',str(tmp_path/'none'),
        '--work-root',str(tmp_path/'new'),'--run-rehearsal'])==1
    assert not (tmp_path/'new').exists()


def test_failure_keeps_original_zip_and_writes_failure_evidence(tmp_path,monkeypatch):
    archive=make_zip(tmp_path); original=archive.read_bytes()
    monkeypatch.setattr(rehearsal,'Tools',lambda *args:SimpleNamespace())
    def fail(*args): raise rehearsal.RehearsalError('Fixture source export failed safely')
    monkeypatch.setattr(rehearsal,'capture',fail)
    with pytest.raises(rehearsal.RehearsalError): rehearsal.rehearse(archive,tmp_path,tmp_path/'work')
    reports=list((tmp_path/'work').glob('run-*/result.json'))
    assert len(reports)==1 and not json.loads(reports[0].read_text())['passed']
    assert archive.read_bytes()==original


@pytest.mark.parametrize('defect',[None,'writable','isolation','different-source','sequence-change','export-failure'])
def test_capture_binds_dump_and_controls_to_one_read_only_snapshot(tmp_path,monkeypatch,defect):
    import database
    calls=[]
    class Source:
        def cursor(self): return self
        def __enter__(self): return self
        def __exit__(self,*args): pass
        def execute(self,statement): self.statement=statement; calls.append(statement)
        def fetchone(self):
            if self.statement=='SHOW transaction_read_only': return ('off' if defect=='writable' else 'on',)
            if self.statement=='SHOW transaction_isolation': return ('read committed' if defect=='isolation' else 'repeatable read',)
            return ('fixture-snapshot',)
        def get_dsn_parameters(self): return {'host':'other.invalid' if defect=='different-source' else 'forbidden.invalid','port':'5432','dbname':'production'}
    native=[]
    class Tool:
        def run(self,name,args,env):
            native.append((name,args,env))
            if defect=='export-failure': raise rehearsal.RehearsalError('Fixture export failure')
            Path(args[args.index('--file')+1]).write_bytes(b'PGDMPsynthetic')
    baseline={'tables':{},'metadata':{},'sequences':[2 if defect=='sequence-change' else 1],
              'accounting_totals':[],'accounting_amount_columns':[]}
    monkeypatch.setattr(database,'run_read_snapshot_pg',lambda callback:callback(Source()))
    monkeypatch.setattr(rehearsal,'sequence_states',lambda conn:[1])
    monkeypatch.setattr(rehearsal,'inventory',lambda conn:{'fixture':True})
    monkeypatch.setattr(rehearsal,'controls',lambda conn:baseline)
    if defect:
        with pytest.raises(rehearsal.RehearsalError): rehearsal.capture(Tool(),tmp_path)
    else:
        dump,expected,info=rehearsal.capture(Tool(),tmp_path)
        assert dump.is_file() and expected==baseline and info=={'fixture':True}
        name,args,env=native[0]
        assert name=='pg_dump' and args[args.index('--snapshot')+1]=='fixture-snapshot'
        assert not any(str(a).startswith(('--schema','--table','--exclude','--data-only')) for a in args)
        assert 'default_transaction_read_only=on' in env['PGOPTIONS']
        assert all(statement.startswith(('SELECT','SHOW','SET LOCAL')) for statement in calls)


@pytest.mark.skipif(not os.environ.get('TEST_POSTGRES_BIN_DIR'),reason='Optional real PostgreSQL fixture; never uses source pool or Drive')
@pytest.mark.parametrize('extension_schema',[None,'public','extensions'])
def test_native_full_recovery_retains_rows_sequences_settings_and_permissions(monkeypatch,extension_schema,tmp_path):
    # Short external paths avoid Windows MAX_PATH; this owns both test clusters.
    with tempfile.TemporaryDirectory(prefix='auditrec-') as temporary:
        root=Path(temporary); source_work=root/'source'; source_work.mkdir()
        tools=rehearsal.Tools(os.environ['TEST_POSTGRES_BIN_DIR'],source_work)
        initial={'locale':{'provider':'c','collate':'C','ctype':'C','locale':None,'rules':None,'encoding':'UTF8'},
            'roles':[],'extensions':[],'schema_owner':'postgres'}
        if extension_schema=='public':
            initial['locale'].update(provider='i',locale='en-US')
            initial['schema_owner']='pg_database_owner'
        with rehearsal.Cluster(tools,source_work) as cluster:
            name='backup_rehearsal_fixture'; cluster.create(name,initial)
            with cluster.connection(name) as conn:
                with conn.cursor() as cur:
                    cur.execute('CREATE ROLE fixture_reader NOLOGIN')
                    cur.execute('CREATE TABLE client_data (id bigint GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,client_id int,template_id int,derived_debit double precision,derived_credit double precision,row_data jsonb)')
                    cur.executemany('INSERT INTO client_data(client_id,template_id,derived_debit,derived_credit,row_data) VALUES (1,1,%s,0,%s)',
                        [('123.450001','{"voucher":"SAME","note":"Keep repeated lines"}')]*685)
                    cur.execute('CREATE TABLE exact_repeats(voucher text,amount numeric)')
                    cur.executemany('INSERT INTO exact_repeats VALUES (%s,%s)',[('IDENTICAL','123.450001')]*3)
                    cur.execute('CREATE TABLE independent_settings(name text,value jsonb)')
                    cur.execute('INSERT INTO independent_settings VALUES (%s,%s)',('KEEP-SETTINGS','{"precision":6}'))
                    if extension_schema:
                        if extension_schema!='public': cur.execute('CREATE SCHEMA extensions')
                        cur.execute('CREATE ROLE fixture_extension_owner NOLOGIN SUPERUSER')
                        cur.execute('ALTER DEFAULT PRIVILEGES FOR ROLE fixture_extension_owner GRANT EXECUTE ON FUNCTIONS TO fixture_reader')
                        cur.execute('ALTER DEFAULT PRIVILEGES FOR ROLE fixture_extension_owner IN SCHEMA public GRANT EXECUTE ON FUNCTIONS TO fixture_reader')
                        cur.execute('SET LOCAL ROLE fixture_extension_owner')
                        cur.execute(rehearsal.sql.SQL('CREATE EXTENSION citext WITH SCHEMA {}').format(rehearsal.sql.Identifier(extension_schema)))
                        cur.execute('RESET ROLE')
                        cur.execute('ALTER ROLE fixture_extension_owner NOSUPERUSER')
                        cur.execute(rehearsal.sql.SQL('ALTER TABLE independent_settings ADD COLUMN note {}.citext DEFAULT %s').format(rehearsal.sql.Identifier(extension_schema)),('CaSe',))
                    cur.execute('ALTER TABLE exact_repeats ENABLE ROW LEVEL SECURITY')
                    cur.execute('CREATE POLICY readable ON exact_repeats FOR SELECT TO fixture_reader USING (true)')
                    cur.execute('GRANT SELECT ON ALL TABLES IN SCHEMA public TO fixture_reader')
                    cur.execute('GRANT USAGE ON SCHEMA public TO fixture_reader')
                    cur.execute('ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO fixture_reader')
                conn.commit()
                expected=rehearsal.controls(conn); info=rehearsal.inventory(conn)
                with conn.cursor() as cur:
                    cur.execute("SELECT encode(sha256(convert_to(to_jsonb(t)::text,'UTF8')),'hex') AS h FROM exact_repeats t ORDER BY h")
                    assert rehearsal.digest_rows(cur)==expected['tables']['exact_repeats']
            native=root/'fixture.dump'; tools.run('pg_dump',['--format=custom','--file',native],cluster.env(name))
            archive=make_zip(root,content=native.read_bytes())
            captures=[]
            def capture_fixture(destination_tools,work):
                # No production snapshot helper is called; fake only the source
                # acquisition, then run actual initdb/restore/query/comparison.
                captures.append(True)
                fresh=work/'fresh.dump'; shutil.copyfile(native,fresh)
                return fresh,expected,info
            monkeypatch.setattr(rehearsal,'capture',capture_fixture)
            monkeypatch.setattr(rehearsal,'snapshot_controls',lambda:(expected,info))
            try:
                result=rehearsal.rehearse(archive,tools.directory,root/'recovery',compare_current=extension_schema=='public')
            except rehearsal.RehearsalError:
                rehearsal.write_report(tmp_path/'expected-controls.json',expected)
                for p in (root/'recovery').glob('run-*/*restored-controls.json'):
                    shutil.copyfile(p,tmp_path/p.name)
                raise
            assert result['passed'] and result['comparison']['application_rows']==689
            assert result['comparison']['accounting_totals_checked']
            assert result['comparison']['sequence_count']==1
            assert len(captures)==(0 if extension_schema=='public' else 1)
            with cluster.connection(name) as conn: assert rehearsal.controls(conn)==expected
            assert not result['managed_supabase_services_verified'] and not result['working_database_restore']
            assert not list((root/'recovery').glob('run-*/cluster/postmaster.pid'))
