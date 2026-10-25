"""Operator-only recovery proof. Writes only to a newly created loopback cluster.

An existing completed ZIP proves archive usability. A separate read-only exported
snapshot binds a fresh dump to its table controls; yesterday is never compared
with today's changing rows. Supabase-managed services are outside certification.
"""
import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import socket
import subprocess
import sys
import uuid
import zipfile

import psycopg2
from psycopg2 import sql

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from services import database_backup as backup

PROJECT = Path(__file__).resolve().parents[1]
NAME = re.compile(r'^backup_rehearsal_[a-z0-9_]{1,40}$')
MAX_ARCHIVE = 256 * 1024**2


class RehearsalError(ValueError):
    """Safe operator message; native diagnostics remain in protected storage."""


def progress(message):
    print(message, flush=True)


def write_report(path, value):
    with path.open('x', encoding='utf-8') as out:
        json.dump(value, out, sort_keys=True, indent=2, default=str)


def external(path):
    path = Path(path)
    if not path.is_absolute():
        raise RehearsalError('Use an absolute protected folder outside the application.')
    resolved = path.resolve()
    if resolved == PROJECT or PROJECT in resolved.parents:
        raise RehearsalError('Rehearsal data must stay outside the application checkout.')
    if path.is_symlink() or resolved != path.absolute():
        raise RehearsalError('Rehearsal paths must not use links or junctions.')
    return resolved


def verified_archive(path, output):
    """Read only a protected completed app backup; never extract ZIP filenames."""
    path = external(path)
    if not path.is_file() or path.stat().st_size > MAX_ARCHIVE:
        raise RehearsalError('Backup missing or larger than the reviewed 256-MiB limit.')
    receipt = json.loads((path.parent / 'receipt.json').read_text(encoding='utf-8'))
    if (receipt.get('state') != 'complete' or receipt.get('file_name') != path.name
            or receipt.get('bytes') != path.stat().st_size or backup._sha256(path) != receipt.get('sha256')):
        raise RehearsalError('The backup does not match its completed receipt.')
    with zipfile.ZipFile(path) as archive:
        members = archive.infolist()
        if sorted(m.filename for m in members) != ['database.dump', 'manifest.json']:
            raise RehearsalError('Unexpected or repeated ZIP members; nothing extracted.')
        if archive.getinfo('manifest.json').file_size > 16384:
            raise RehearsalError('Backup manifest is too large.')
        manifest = json.loads(archive.read('manifest.json'))
        if (manifest.get('format') != 'postgresql-custom' or manifest.get('version') != 1
                or manifest.get('operation_id') != receipt.get('operation_id')
                or manifest.get('dump_file') != 'database.dump'
                or archive.getinfo('database.dump').file_size != manifest.get('dump_bytes')
                or not 5 <= manifest['dump_bytes'] <= MAX_ARCHIVE):
            raise RehearsalError('The archive scope/size manifest is invalid.')
        with archive.open('database.dump') as source, output.open('xb') as target:
            shutil.copyfileobj(source, target, length=1024**2)
    with output.open('rb') as native:
        magic = native.read(5)
    if backup._sha256(output) != manifest.get('dump_sha256') or magic != b'PGDMP':
        raise RehearsalError('Embedded database archive checksum/format failed.')
    return {'zip_sha256': receipt['sha256'], 'dump_sha256': manifest['dump_sha256'],
            'operation_id': receipt['operation_id'], 'bytes': receipt['bytes']}


class Tools:
    def __init__(self, directory, work):
        self.directory, self.work, self.number = Path(directory).resolve(), work, 0
        for name in ('pg_dump', 'pg_restore', 'initdb', 'pg_ctl'):
            if not self.path(name).is_file():
                raise RehearsalError('Existing PostgreSQL tools are incomplete; no tools were installed.')

    def path(self, name):
        return self.directory / (name + ('.exe' if os.name == 'nt' else ''))

    def run(self, name, args, env=None, *, timeout=900):
        self.number += 1
        diagnostic = self.work / f'tool-{self.number:03d}-{name}.log'
        # File-backed handles prevent Windows server children holding PIPE open.
        with diagnostic.open('xb') as out:
            try:
                subprocess.run([str(self.path(name)), *map(str, args)],
                    env=env or backup._process_env(), stdin=subprocess.DEVNULL,
                    stdout=out, stderr=subprocess.STDOUT, check=True, timeout=timeout,
                    creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            except subprocess.TimeoutExpired:
                raise RehearsalError(f'{name} exceeded its {timeout}-second limit. Partial artifacts retained; recovery is not certified.') from None
            except (subprocess.SubprocessError, OSError):
                raise RehearsalError(f'{name} failed; see its protected diagnostic. No working database was restored.') from None
        return diagnostic


def check_target(conn, cluster, database, *, allow_admin=False):
    if not NAME.fullmatch(database) and not (allow_admin and database == 'postgres'):
        raise RehearsalError('Only a named backup_rehearsal_* target is accepted.')
    with conn.cursor() as cur:
        cur.execute("SELECT current_database(),inet_server_addr()::text,inet_client_addr()::text,current_setting('data_directory')")
        name, server, client, directory = cur.fetchone()
    try:
        loopback = all(ipaddress.ip_address(x.split('/')[0]).is_loopback for x in (server, client))
    except (ValueError, AttributeError):
        loopback = False
    if (name != database or not loopback
            or Path(directory).resolve() != cluster.data.resolve()
            or (cluster.work / 'cluster-marker').read_text() != cluster.marker):
        raise RehearsalError('Target is not this run’s new isolated local cluster.')


class Cluster:
    def __init__(self, tools, work):
        self.tools, self.work, self.data = tools, work, work / 'cluster'
        self.marker, self.password = uuid.uuid4().hex, secrets.token_urlsafe(32)
        with socket.socket() as reservation:
            reservation.bind(('127.0.0.1', 0)); self.port = reservation.getsockname()[1]
        self.started = False

    def env(self, database):
        return backup._process_env() | {'PGHOST': '127.0.0.1', 'PGPORT': str(self.port),
            'PGUSER': 'postgres', 'PGPASSWORD': self.password, 'PGDATABASE': database,
            'PGSSLMODE': 'disable', 'PGCONNECT_TIMEOUT': '5'}

    @contextmanager
    def connection(self, database):
        # libpq otherwise reads ambient PGSERVICE/PGHOSTADDR even with host=.
        # This standalone operator process has no concurrent app requests.
        inherited = {k:v for k,v in os.environ.items() if k.upper().startswith('PG')}
        try:
            for key in inherited: os.environ.pop(key, None)
            conn = psycopg2.connect(host='127.0.0.1', hostaddr='127.0.0.1', port=self.port, user='postgres',
                password=self.password, dbname=database, sslmode='disable', connect_timeout=5)
        finally:
            os.environ.update(inherited)
        try:
            yield conn
        finally:
            conn.close()

    def __enter__(self):
        if self.data.exists():
            raise RehearsalError('A rehearsal must use a fresh cluster directory.')
        (self.work / 'cluster-marker').write_text(self.marker)
        password_file = self.work / 'initial-password.tmp'
        descriptor = os.open(password_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, 'w') as out: out.write(self.password)
            self.tools.run('initdb', ['-D', self.data, '-U', 'postgres', '-A', 'scram-sha-256',
                '--pwfile', password_file, '--encoding=UTF8', '--no-locale'])
        finally:
            password_file.unlink(missing_ok=True)
        try:
            self.tools.run('pg_ctl', ['-D', self.data, '-l', self.work / 'server.log',
                '-o', f'-h 127.0.0.1 -p {self.port}', '-w', 'start'], timeout=90)
            self.started = True
            return self
        except BaseException:
            self.stop()
            raise

    def stop(self):
        if (self.data / 'postmaster.pid').exists():
            self.tools.run('pg_ctl', ['-D', self.data, '-m', 'fast', '-w', 'stop'], timeout=90)
        self.started = False

    def __exit__(self, *args):
        self.stop()

    def create(self, name, inventory, *, extensions=True):
        if not NAME.fullmatch(name):
            raise RehearsalError('Refusing an ordinary or supplied production database name.')
        locale = inventory['locale']; provider = {'i': 'icu', 'c': 'libc', 'b': 'builtin'}.get(locale['provider'])
        if provider is None: raise RehearsalError('Unsupported collation provider.')
        with self.connection('postgres') as conn:
            # The admin connection is generated internally, never from DATABASE_URL.
            check_target(conn, self, 'postgres', allow_admin=True)
            conn.rollback(); conn.autocommit = True
            with conn.cursor() as cur:
                for role in inventory['roles']:
                    cur.execute('SELECT 1 FROM pg_roles WHERE rolname=%s', (role,))
                    if not cur.fetchone():
                        cur.execute(sql.SQL('CREATE ROLE {} NOLOGIN').format(sql.Identifier(role)))
                query = sql.SQL('CREATE DATABASE {} TEMPLATE template0 ENCODING {} LOCALE_PROVIDER {}').format(
                    sql.Identifier(name), sql.Literal(locale['encoding']), sql.SQL(provider))
                if provider == 'icu':
                    query += sql.SQL(' ICU_LOCALE {}').format(sql.Literal(locale['locale']))
                    if locale['rules']: query += sql.SQL(' ICU_RULES {}').format(sql.Literal(locale['rules']))
                elif provider == 'builtin':
                    query += sql.SQL(' BUILTIN_LOCALE {}').format(sql.Literal(locale['locale']))
                else:
                    query += sql.SQL(' LC_COLLATE {} LC_CTYPE {}').format(sql.Literal(locale['collate']), sql.Literal(locale['ctype']))
                cur.execute(query)
        with self.connection(name) as conn:
            check_target(conn, self, name)
            actual_locale = inventory_locale(conn)
            protected = ['provider','encoding'] + (['locale','rules'] if provider in ('icu','builtin') else ['collate','ctype'])
            if any(actual_locale[k] != locale[k] for k in protected):
                raise RehearsalError('The local database collation does not match the source comparison rules.')
            with conn.cursor() as cur:
                cur.execute("SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='public'")
                if cur.fetchone()[0]: raise RehearsalError('New target schema is not empty.')
                # Retain template0's standard public USAGE baseline; pg_dump's
                # schema ACL commands describe changes from that baseline.
                cur.execute(sql.SQL('ALTER SCHEMA public OWNER TO {}').format(sql.Identifier(inventory['schema_owner'])))
                if extensions: self.install_extensions(cur, inventory['extensions'])
            conn.commit()

    @staticmethod
    def install_extensions(cur, extensions):
        for extension in extensions:
            if extension['name'] != 'citext':
                raise RehearsalError('An unreviewed application extension requires review.')
            cur.execute(sql.SQL('CREATE SCHEMA IF NOT EXISTS {}').format(sql.Identifier(extension['schema'])))
            owner = extension['owner']
            if owner != 'postgres':
                # NOLOGIN placeholders in this new cluster; temporary installer
                # power is revoked before committing the extension setup.
                cur.execute(sql.SQL('ALTER ROLE {} SUPERUSER').format(sql.Identifier(owner)))
            cur.execute(sql.SQL('SET LOCAL ROLE {}').format(sql.Identifier(owner)))
            cur.execute(sql.SQL('CREATE EXTENSION {} WITH SCHEMA {} VERSION {}').format(
                sql.Identifier(extension['name']), sql.Identifier(extension['schema']), sql.Literal(extension['version'])))
            cur.execute('RESET ROLE')
            if owner != 'postgres':
                cur.execute(sql.SQL('ALTER ROLE {} NOSUPERUSER').format(sql.Identifier(owner)))

    def restore_metadata(self, dump, name, lines, pattern, label):
        chosen = [line for line in lines if re.match(pattern, line)]
        if not chosen: return
        path = self.work / (name + '-' + label + '.txt')
        with path.open('x', encoding='utf-8') as out: out.write('\n'.join(chosen) + '\n')
        with self.connection(name) as conn: check_target(conn, self, name)
        self.tools.run('pg_restore', ['--exit-on-error','--single-transaction','--no-password',
            '--use-list',path,'--dbname',name,dump],self.env(name))

    def restore(self, dump, name, inventory):
        toc = self.tools.run('pg_restore', ['--list', dump])
        lines = toc.read_text(encoding='utf-8').splitlines()
        schemas = [line for line in lines if re.match(r'^\d+; \d+ \d+ SCHEMA - public ',line)]
        # pg_dump omits the standard public declaration owned by
        # pg_database_owner; template0 supplies exactly that default.
        standard = not schemas and inventory['schema_owner']=='pg_database_owner'
        if not standard and (len(schemas)!=1 or not schemas[0].endswith(' ' + inventory['schema_owner'])):
            raise RehearsalError('Archived schema owner differs from the reviewed environment; it was not rewritten.')
        self.create(name, inventory, extensions=False)
        # Only the reviewed extension owners' function defaults are needed before
        # installation. Table/sequence defaults stay after object creation, as in
        # pg_restore's normal ordering, so old objects gain no invented grants.
        owners = '|'.join(re.escape(e['owner']) for e in inventory['extensions'])
        if owners:
            self.restore_metadata(dump,name,lines,
                r'^\d+; \d+ \d+ DEFAULT ACL (?:-|public) DEFAULT PRIVILEGES FOR FUNCTIONS (?:'+owners+r')$',
                'extension-function-defaults')
        with self.connection(name) as conn:
            check_target(conn,self,name)
            with conn.cursor() as cur: self.install_extensions(cur,inventory['extensions'])
            conn.commit()
        # Public must exist before a public citext extension. Omit only its
        # already-created SCHEMA declaration; its ACL/comments and all data stay.
        selected = self.work / (name + '-toc.txt')
        with selected.open('x', encoding='utf-8') as out:
            out.write('\n'.join(line for line in lines if not re.match(r'^\d+; \d+ \d+ SCHEMA - public ', line)) + '\n')
        # Preserve owners/ACLs; no --clean, --create, trigger disabling or row filters.
        # The complete archive stays intact; only the application schema is certified.
        self.tools.run('pg_restore', ['--exit-on-error', '--single-transaction', '--schema=public',
            '--use-list', selected, '--no-password', '--dbname', name, dump], self.env(name))
        self.restore_metadata(dump,name,lines,r'^\d+; \d+ \d+ DEFAULT ACL - ','global-defaults')
        self.restore_metadata(dump,name,lines,r'^\d+; \d+ \d+ (ACL|COMMENT) - SCHEMA public ','schema-grants')
        with self.connection(name) as conn:
            check_target(conn, self, name)
            return controls(conn)


def query(conn, statement, params=()):
    with conn.cursor() as cur:
        cur.execute(statement, params or None)
        columns = [c.name for c in cur.description]
        return [dict(zip(columns, row)) for row in cur.fetchall()]


def inventory_locale(conn):
    return query(conn, """SELECT datlocprovider AS provider,datcollate AS collate,
        datctype AS ctype,datlocale AS locale,daticurules AS rules,pg_encoding_to_char(encoding) AS encoding
        FROM pg_database WHERE datname=current_database()""")[0]


def inventory(conn):
    locale = inventory_locale(conn)
    roles = [r['rolname'] for r in query(conn, 'SELECT rolname FROM pg_roles ORDER BY rolname')]
    extensions = query(conn, """SELECT e.extname AS name,n.nspname AS schema,e.extversion AS version,pg_get_userbyid(e.extowner) AS owner
        FROM pg_extension e JOIN pg_namespace n ON n.oid=e.extnamespace WHERE e.extname='citext' ORDER BY 1""")
    schema_owner = query(conn, "SELECT pg_get_userbyid(nspowner) AS owner FROM pg_namespace WHERE nspname='public'")[0]['owner']
    return {'locale': locale, 'roles': roles, 'extensions': extensions, 'schema_owner': schema_owner}


METADATA = {
    'relations': """SELECT c.relname,c.relkind,pg_get_userbyid(c.relowner) AS owner,c.relpersistence,
        c.relrowsecurity,c.relforcerowsecurity,c.reloptions,
        ARRAY(SELECT a::text FROM unnest(c.relacl) a ORDER BY a::text) AS relacl FROM pg_class c
        JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='public'
        AND c.relkind IN ('r','p','v','m','S','f') ORDER BY c.relname""",
    'columns': """SELECT table_name,column_name,ordinal_position,column_default,is_nullable,data_type,
        udt_schema,udt_name,collation_name,is_identity,identity_generation,is_generated,generation_expression
        FROM information_schema.columns WHERE table_schema='public' ORDER BY table_name,ordinal_position""",
    'constraints': """SELECT c.relname,k.conname,pg_get_constraintdef(k.oid) AS definition,k.convalidated
        FROM pg_constraint k JOIN pg_class c ON c.oid=k.conrelid JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname='public' ORDER BY c.relname,k.conname""",
    'indexes': """SELECT tablename,indexname,indexdef FROM pg_indexes WHERE schemaname='public' ORDER BY tablename,indexname""",
    'triggers': """SELECT c.relname,t.tgname,t.tgenabled,pg_get_triggerdef(t.oid) AS definition FROM pg_trigger t
        JOIN pg_class c ON c.oid=t.tgrelid JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname='public' AND NOT t.tgisinternal ORDER BY c.relname,t.tgname""",
    'policies': "SELECT * FROM pg_policies WHERE schemaname='public' ORDER BY tablename,policyname",
    'grants': """SELECT table_name,column_name,grantor,grantee,privilege_type,is_grantable
        FROM information_schema.column_privileges WHERE table_schema='public'
        ORDER BY table_name,column_name,grantor,grantee,privilege_type""",
    'views': "SELECT viewname,viewowner,definition FROM pg_views WHERE schemaname='public' ORDER BY viewname",
    'functions': """SELECT p.proname,pg_get_function_identity_arguments(p.oid) AS arguments,
        pg_get_functiondef(p.oid) AS definition,pg_get_userbyid(p.proowner) AS owner,
        ARRAY(SELECT a::text FROM unnest(p.proacl) a ORDER BY a::text) AS proacl
        FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
        WHERE n.nspname='public' AND p.prokind IN ('f','p') ORDER BY 1,2""",
    'defaults': """SELECT pg_get_userbyid(d.defaclrole) AS owner,n.nspname,d.defaclobjtype,
        ARRAY(SELECT a::text FROM unnest(d.defaclacl) a ORDER BY a::text) AS defaclacl
        FROM pg_default_acl d LEFT JOIN pg_namespace n ON n.oid=d.defaclnamespace
        WHERE n.nspname='public' OR n.oid IS NULL ORDER BY 1,2 NULLS FIRST,3""",
    'schema': """SELECT pg_get_userbyid(nspowner) AS owner,
        ARRAY(SELECT a::text FROM unnest(nspacl) a ORDER BY a::text) AS nspacl FROM pg_namespace WHERE nspname='public'""",
    'types': """SELECT t.typname,t.typtype,pg_get_userbyid(t.typowner) AS owner,
        format_type(t.typbasetype,t.typtypmod) AS base_type,t.typnotnull,t.typdefault,
        (SELECT array_agg(e.enumlabel ORDER BY e.enumsortorder) FROM pg_enum e WHERE e.enumtypid=t.oid) AS labels
        FROM pg_type t JOIN pg_namespace n ON n.oid=t.typnamespace
        WHERE n.nspname='public' AND t.typtype IN ('d','e') ORDER BY t.typname""",
}


def sequence_states(conn):
    rows = query(conn, """SELECT sequencename,sequenceowner,data_type,start_value,min_value,max_value,
        increment_by,cycle,cache_size FROM pg_sequences WHERE schemaname='public' ORDER BY sequencename""")
    for row in rows:
        row.update(query(conn, sql.SQL('SELECT last_value,is_called FROM public.{}').format(sql.Identifier(row['sequencename'])))[0])
    return rows


def digest_rows(values):
    """Fixed-size row hashes retain multiplicity; never distinct/deduplicate rows."""
    digest = hashlib.sha256(); count = repeats = 0; previous = None
    for value in values:
        fingerprint = value[0]
        digest.update(bytes.fromhex(fingerprint)); count += 1
        repeats += fingerprint == previous; previous = fingerprint
    return {'rows': count, 'sha256': digest.hexdigest(), 'exact_repeat_rows': repeats}


def controls(conn):
    with conn.cursor() as cur:
        cur.execute("SET LOCAL TimeZone='UTC'; SET LOCAL extra_float_digits=3; SET LOCAL DateStyle='ISO, YMD'; SET LOCAL row_security=off")
    metadata = {name: query(conn, statement) for name, statement in METADATA.items()}
    tables = query(conn, """SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname='public' AND c.relkind='r' ORDER BY c.relname""")
    table_controls = {}
    for index, item in enumerate(tables):
        progress(f'Checking complete application table {index+1} of {len(tables)}')
        # Same multiplicity-preserving seal as digest_rows, computed at source.
        # Only one compact result crosses the weak connection. DISTINCT is used
        # only to count repeated complete rows, never to filter the fingerprint.
        table_controls[item['relname']] = query(conn, sql.SQL("""SELECT count(*) AS rows,
            encode(sha256(coalesce(string_agg(decode(h,'hex'),''::bytea ORDER BY h),''::bytea)),'hex') AS sha256,
            count(*)-count(DISTINCT h) AS exact_repeat_rows FROM
            (SELECT encode(sha256(convert_to(to_jsonb(t)::text,'UTF8')),'hex') AS h FROM public.{} t) complete_rows""").format(sql.Identifier(item['relname'])))[0]
    financial = []
    columns = [r for r in metadata['columns'] if r['table_name']=='client_data']
    names = {r['column_name'] for r in columns}
    amounts = [r['column_name'] for r in columns if r['data_type'] in ('numeric','double precision','real')
               and r['column_name'] in ('derived_debit','derived_credit')]
    if amounts and {'client_id','template_id'} <= names:
        grouping = ['client_id','template_id'] + [n for n in
            ('derived_transaction_month','is_deleted','change_detection_status') if n in names]
        keys = sql.SQL(',').join(map(sql.Identifier, grouping))
        sums = sql.SQL(',').join(sql.SQL('sum({}::numeric)::text AS {}').format(sql.Identifier(n),sql.Identifier(n)) for n in amounts)
        financial = query(conn, sql.SQL('SELECT {},count(*) AS lines,{} FROM public.client_data GROUP BY {} ORDER BY {}').format(keys,sums,keys,keys))
    return {'tables': table_controls, 'metadata': metadata, 'sequences': sequence_states(conn),
            'accounting_totals': financial, 'accounting_amount_columns': amounts,
            'accounting_scope': 'All stored canonical lines, grouped by client/template and available month/row state; not report presentation totals.'}


def compare(expected, restored):
    # JSON normalizes Decimal/date values identically without changing row data.
    clean = lambda value: json.loads(json.dumps(value, sort_keys=True, default=str))
    differences = [key for key in expected if clean(expected[key]) != clean(restored.get(key))]
    return {'passed': not differences, 'different_sections': differences,
            'application_tables': len(expected['tables']),
            'application_rows': sum(t['rows'] for t in expected['tables'].values()),
            'sequence_count': len(expected['sequences']), 'accounting_totals_checked': bool(expected['accounting_amount_columns'])}


def capture(tools, work):
    # Follow the established caller-supplied snapshot boundary; never borrow a pool
    # connection directly or reuse the live URL as any restore target.
    from database import run_read_snapshot_pg
    def read(conn):
        attempt = work / ('source-' + uuid.uuid4().hex[:8]); attempt.mkdir()
        with conn.cursor() as cur:
            cur.execute("SET LOCAL idle_in_transaction_session_timeout='900s'; SET LOCAL statement_timeout='120s'; SET LOCAL lock_timeout='5s'")
            cur.execute('SHOW transaction_read_only')
            if cur.fetchone()[0] != 'on': raise RehearsalError('Source snapshot is not read-only.')
            cur.execute('SHOW transaction_isolation')
            if cur.fetchone()[0] != 'repeatable read': raise RehearsalError('Source controls require repeatable-read isolation.')
            cur.execute('SELECT pg_export_snapshot()'); snapshot = cur.fetchone()[0]
        before_sequences = sequence_states(conn)
        info = inventory(conn)
        # Preserve the small setup inventory even when a weak connection prevents
        # a complete export. This is not a row baseline or a passed comparison.
        write_report(attempt / 'inventory.json', info)
        env = backup._connection_env(); connected = conn.get_dsn_parameters()
        if any(env.get(key, fallback) != connected.get(field, fallback) for key, field, fallback in
               [('PGHOST','host',''),('PGPORT','port','5432'),('PGDATABASE','dbname','')]):
            raise RehearsalError('The backup and read-only control connections must identify the same source.')
        progress('Saving one additional read-only snapshot for exact comparison')
        dump = attempt / 'comparison.dump'
        tools.run('pg_dump', ['--format=custom','--no-password','--snapshot',snapshot,
            '--lock-wait-timeout=10000','--file',dump], env)
        baseline = controls(conn)
        if before_sequences != baseline['sequences']:
            raise RehearsalError('Source numbering counters changed during capture; comparison is inconclusive. Retry during a quiet period.')
        write_report(attempt / 'source-controls.json', baseline)
        return dump, baseline, info
    return run_read_snapshot_pg(read)


def snapshot_controls():
    """Compact current-state proof; equality is required, never assumed for old ZIPs."""
    from database import run_read_snapshot_pg
    def read(conn):
        with conn.cursor() as cur:
            cur.execute("SET LOCAL statement_timeout='120s'; SET LOCAL idle_in_transaction_session_timeout='120s'")
            cur.execute('SHOW transaction_read_only')
            if cur.fetchone()[0]!='on': raise RehearsalError('Source comparison must be read-only.')
            cur.execute('SHOW transaction_isolation')
            if cur.fetchone()[0]!='repeatable read': raise RehearsalError('Source comparison must use repeatable-read isolation.')
        before = sequence_states(conn)
        info, baseline = inventory(conn), controls(conn)
        if before != baseline['sequences']:
            raise RehearsalError('Source numbering counters changed; comparison is inconclusive.')
        return baseline, info
    return run_read_snapshot_pg(read)


def rehearse(archive, tools_dir, work_root, *, compare_current=False):
    root = external(work_root)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if shutil.disk_usage(root).free < 3 * 1024**3:
        raise RehearsalError('At least 3 GiB of free protected local disk space is required.')
    work = root / ('run-' + uuid.uuid4().hex)
    work.mkdir(mode=0o700)
    result = {'passed': False, 'scope': 'public_application_schema_only',
              'managed_supabase_services_verified': False, 'working_database_restore': False,
              'created_at': datetime.now(timezone.utc).isoformat()}
    try:
        progress('Checking the existing completed ZIP and embedded archive')
        old_dump = work / 'accepted.dump'
        result['accepted_archive'] = verified_archive(archive, old_dump)
        tools = Tools(tools_dir, work)
        if compare_current:
            progress('Reading compact controls from one read-only source snapshot; no new export')
            baseline, info = snapshot_controls()
            write_report(work / 'source-controls.json',baseline)
            write_report(work / 'inventory.json',info)
            result['comparison_method'] = 'accepted_archive_vs_current_read_only_snapshot'
        else:
            fresh_dump, baseline, info = capture(tools, work)
            result['comparison_method'] = 'synchronised_fresh_dump_and_source_controls'
        if 'client_data' in baseline['tables'] and not baseline['accounting_amount_columns']:
            raise RehearsalError('Canonical debit/credit controls are unavailable; recovery totals are not certified.')
        with Cluster(tools, work) as cluster:
            progress('Restoring the accepted backup into a new local test database')
            old = cluster.restore(old_dump, 'backup_rehearsal_accepted', info)
            write_report(work / 'accepted-restored-controls.json', old)
            result['accepted_archive_restore'] = {'passed': True, 'application_tables': len(old['tables']),
                'application_rows': sum(t['rows'] for t in old['tables'].values()),
                'limitation': ('Original-time controls were not stored; this mode requires exact equality with one current frozen source state.'
                    if compare_current else 'No original-time table controls exist for this older ZIP; not compared with today.')}
            if compare_current:
                restored = old
            else:
                progress('Restoring the synchronised snapshot into another new local test database')
                restored = cluster.restore(fresh_dump, 'backup_rehearsal_comparison', info)
            write_report(work / 'comparison-restored-controls.json', restored)
            result['comparison'] = compare(baseline, restored)
            if not result['comparison']['passed']:
                message = ('The accepted ZIP does not exactly match current data/settings; legitimate later changes may explain this. Use the paired-snapshot mode when needed.'
                           if compare_current else 'Restored controls differ. Evidence retained; no recovery certification issued.')
                raise RehearsalError(message)
            result['passed'] = True
    except Exception as error:
        result['passed'] = False
        result['error'] = str(error) if isinstance(error, RehearsalError) else 'Rehearsal failed; protected artifacts retained. Working data was not restored or replaced.'
        raise
    finally:
        result['finished_at'] = datetime.now(timezone.utc).isoformat()
        write_report(work / 'result.json', result)
        progress('Recovery evidence: ' + str(work / 'result.json'))
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--archive', type=Path, required=True)
    parser.add_argument('--tools', type=Path, required=True)
    parser.add_argument('--work-root', type=Path, required=True)
    parser.add_argument('--compare-current', action='store_true', help='Compare the accepted ZIP with compact current source controls, without another export. Only an exact match passes.')
    parser.add_argument('--run-rehearsal', action='store_true', help='Explicitly authorise the read-only source export and new isolated local cluster.')
    args = parser.parse_args(argv)
    if not args.run_rehearsal:
        progress('Preview only. No files/databases changed. Use --run-rehearsal after approval.')
        return 0
    if os.environ.get('RENDER'):
        progress('Recovery rehearsal is unavailable on hosted deployments.')
        return 1
    try:
        result = rehearse(args.archive, args.tools, args.work_root, compare_current=args.compare_current)
        progress('Application recovery comparison passed. Managed Supabase services are not certified.')
        return 0 if result['passed'] else 1
    except Exception:
        progress('Recovery rehearsal did not pass. Working data and the original backup were retained.')
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
