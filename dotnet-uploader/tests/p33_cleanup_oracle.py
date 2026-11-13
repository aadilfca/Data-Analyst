"""Owned PostgreSQL/Python scope parity and lossless purge-backup/re-upload proof."""
import io
import json
import os
import re
import sys
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch
from urllib.request import Request, urlopen
from urllib.parse import urlparse

import openpyxl
import psycopg2
from psycopg2.extras import RealDictCursor

directory = Path(sys.argv[1]).resolve()
fixture = json.loads((directory / 'fixture.json').read_text(encoding='utf-8'))
assert directory.parent.name == '.tools' and directory.name.startswith('p33-')
assert re.fullmatch(r'auditplatform_foundation_[0-9a-f]{32}', fixture['database'])
assert fixture['user'] == 'auditplatform_test' and urlparse(fixture['address']).hostname == '127.0.0.1'
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from services import storage_cleanup as legacy  # noqa: E402

before = json.loads((directory / 'source-before.json').read_text(), parse_float=Decimal)
after = json.loads((directory / 'source-after-reupload.json').read_text(), parse_float=Decimal)
assert before == after and len(after) == 3, 'Real native re-upload must preserve values/order/multiplicity once.'
workbook = openpyxl.load_workbook(directory / 'backup.xlsx', data_only=False)
assert workbook.sheetnames == ['Data', 'Changed-Deleted History']
raw_rows = []
for sheet in workbook:
    assert not any(cell.data_type == 'f' for row in sheet for cell in row)
    found = []
    for row in sheet.iter_rows(min_row=2, values_only=True):
        for cell in row:
            if not isinstance(cell, str) or not cell.startswith('{'):
                continue
            try:
                value = json.loads(cell, parse_float=Decimal)
            except (ValueError, TypeError):
                continue
            if isinstance(value, dict) and 'col_1' in value:
                found.append(value)
    if sheet.title == 'Data':
        raw_rows = found
    else:
        assert found, 'The changed/deleted sheet must preserve actual prior row JSON.'
assert raw_rows == before, 'The backup must contain every purged current row, with original scalar types.'
workbook.close()
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
    request = Request(fixture['address'] + '/api/storage-cleanup/preview?client_id=10&template_id=1&kind=purge',
                      headers={'Cookie': 'session_token=' + 'a' * 64, 'X-App-Context': fixture['context']})
    with urlopen(request, timeout=30) as response:
        native = json.load(response)
    with patch.dict(os.environ, {'DATABASE_URL': 'postgresql://owned-unused'}), patch.object(legacy, 'execute_query_pg', side_effect=query):
        expected = legacy.get_client_data_purge_preview(10, 1)
    for name, count in expected['counts'].items():
        assert native['counts'].get(name) == count, (name, native['counts'].get(name), count)
    for name, count in expected['protected'].items():
        native_name = {'physical_bill_items': 'voucher_line_physical_bill_items', 'prepared_lines': 'cashflow_prepared_lines'}.get(name, name)
        assert native['protected'].get(native_name) == count, (name, native['protected'].get(native_name), count)
    assert native['protected']['cashflow_prepared_lines'] == 0
    assert native['can_apply'] == expected['can_apply'] is True
    assert query('SELECT row_data FROM public.client_data WHERE client_id=20')[0]['row_data'] == {'sentinel': 'preserve foreign exact'}
    assert query('SELECT count(*) n FROM public.trial_balance_accounts')[0]['n'] == 2
    assert query("SELECT count(*) n FROM public.platform_storage_cleanup_runs WHERE state<>'completed'")[0]['n'] == 0
    assert query("SELECT count(*) n FROM public.platform_storage_cleanup_artifacts WHERE state<>'done'")[0]['n'] == 0
    (directory / 'oracle-result.json').write_text(json.dumps({'passed': True, 'reuploaded_rows': 3, 'backup_rows': len(raw_rows),
                                                           'legacy_counts': len(expected['counts'])}), encoding='utf-8')
finally:
    connection.rollback()
    connection.close()
print('P33 oracle passed: real re-upload once, complete raw backup, legacy scope/count parity and preserved foreign/TB data.')
