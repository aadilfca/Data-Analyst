"""Compare native aggregate evidence with Python in the C# test's owned database."""
import json
import re
import sys
from pathlib import Path
from unittest.mock import patch

import psycopg2
from psycopg2.extras import RealDictCursor

directory = Path(sys.argv[1]).resolve()
fixture = json.loads((directory / 'fixture.json').read_text(encoding='utf-8'))
assert directory.parent.name == '.tools' and directory.name.startswith('p32-')
assert re.fullmatch(r'auditplatform_foundation_[0-9a-f]{32}', fixture['database'])
assert fixture['user'] == 'auditplatform_test' and 0 < fixture['port'] < 65536
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from routers import data_integrity as legacy  # noqa: E402

connection = psycopg2.connect(host='127.0.0.1', port=fixture['port'], dbname=fixture['database'],
                              user=fixture['user'], password=fixture['password'], connect_timeout=3)
connection.set_session(readonly=True, isolation_level='REPEATABLE READ')


def query(sql, params=None):
    with connection.cursor(cursor_factory=RealDictCursor) as cursor:
        cursor.execute(sql, params)
        return [dict(row) for row in cursor.fetchall()]


try:
    with connection.cursor() as cursor:
        cursor.execute('SET LOCAL statement_timeout=10000')
        cursor.execute('SHOW transaction_read_only')
        assert cursor.fetchone()[0] == 'on'
    # Native deliberately does not probe arbitrary legacy paths. Match that
    # approved size boundary without changing any record/identity/index oracle.
    with patch.object(legacy, 'execute_query_pg', side_effect=query), patch.object(
            legacy, '_measure_source_files', side_effect=lambda rows: (0, sum(bool(r.get('file_path')) for r in rows))):
        expected = legacy._get_data_integrity_overview(10)
    actual = json.loads((directory / 'large.json').read_text(encoding='utf-8'))
    for native, python in zip(actual['modules'], expected['modules']):
        for key in ('module', 'row_count', 'data_bytes', 'duplicate_groups', 'duplicate_extra_rows',
                    'duplicate_status', 'unidentified_rows'):
            assert native[key] == python[key], (native['module'], key, native[key], python[key])
        if native['module'] != 'Client Data':
            for key in ('associated_record_count', 'associated_data_bytes', 'total_data_bytes', 'size_status'):
                assert native[key] == python[key], (native['module'], key, native[key], python[key])
    for native, python in zip(actual['safeguards']['modules'], expected['safeguards']['modules']):
        assert native['status'] == python['status']
        assert [(c['label'], c['status']) for c in native['checks']] == [(c['label'], c['status']) for c in python['checks']]
    # The old OR upload join includes a foreign upload through an inconsistent
    # link. Native counts only owned metadata and reports those links explicitly.
    assert actual['modules'][0]['associated_record_count'] == 1
    assert expected['modules'][0]['associated_record_count'] == 2
    assert actual['modules'][0]['invalid_upload_links'] == 2
    assert actual['modules'][0]['unmeasured_source_files'] == 1
    with connection.cursor() as cursor:
        cursor.execute('SELECT u.id,pg_column_size(u) FROM public.uploaded_files u WHERE client_id=10 ORDER BY id')
        # Native associated bytes must exclude the foreign row, retaining the
        # accepted per-row composite-size projection rather than an aggregate.
        assert actual['modules'][0]['associated_data_bytes'] == sum(row[1] for row in cursor.fetchall())
    assert actual['modules'][0]['row_count'] == 10005
    assert len(json.dumps(actual).encode()) < 131072
    states = {name: json.loads((directory / (name + '.json')).read_text(encoding='utf-8'))
              for name in ('clean', 'covering', 'impostor', 'limited', 'invalid', 'missing', 'restricted')}
    assert states['clean']['safeguards']['status'] == states['covering']['safeguards']['status'] == 'Passed'
    assert states['impostor']['safeguards']['status'] == 'Needs attention'
    for name in ('limited', 'restricted'):
        assert states[name]['safeguards']['modules'][0]['checks'][0]['status'] == 'Not checked'
    assert states['missing']['modules'][1]['row_count'] is None
    assert states['missing']['modules'][2]['duplicate_groups'] == 1
    (directory / 'oracle-result.json').write_text(json.dumps({'passed': True, 'states': len(states) + 1,
                                                           'large_rows': actual['modules'][0]['row_count']}), encoding='utf-8')
    print('P32 Python oracle: eight states, exact record/duplicate/index parity and explicit native size boundaries passed.')
finally:
    connection.rollback()
    connection.close()
