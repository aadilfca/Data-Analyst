"""Owned SQL/Python history oracle and exact OOXML workbook inspection."""
import json
import sys
import zipfile
from decimal import Decimal
from pathlib import Path

import openpyxl
import psycopg2
from psycopg2.extras import RealDictCursor, register_default_jsonb

directory = Path(sys.argv[1]).resolve()
fixture = json.loads((directory / 'fixture.json').read_text(), parse_float=Decimal)
assert directory.parent.name == '.tools' and directory.name.startswith('p31-')
assert fixture['database'].startswith('auditplatform_foundation_') and fixture['user'] == 'auditplatform_test'
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from routers import data as legacy

connection = psycopg2.connect(host='127.0.0.1', port=fixture['port'], dbname=fixture['database'], user=fixture['user'], password=fixture['password'], connect_timeout=3)
connection.set_session(readonly=True, isolation_level='REPEATABLE READ')
register_default_jsonb(connection, loads=lambda text: json.loads(text, parse_float=Decimal))

def query(sql, params=None, **kwargs):
    with connection.cursor(cursor_factory=RealDictCursor) as cursor:
        cursor.execute(sql, params)
        return [dict(row) for row in cursor.fetchall()]

legacy.execute_query_pg = query
labels = {'col_1': 'Document', 'col_2': 'Date', 'col_3': 'Amount', 'col_4': 'Memo', 'col_5': 'Balance'}
def project(values):
    return None if values is None else {labels.get(key, key): value for key, value in values.items()}

def literal(value):
    # Framework writer escapes OOXML literal _xHHHH_ tokens. openpyxl leaves
    # the inline-string underscore escape visible, unlike Excel/SheetJS.
    return value.replace('_x005F_', '_') if isinstance(value, str) else value

def sheet_rows(sheet):
    headers = [literal(cell.value) for cell in sheet[5]]
    return [dict(zip(headers, [literal(value) for value in row])) for row in sheet.iter_rows(min_row=6, values_only=True)]

try:
    for case in fixture['cases']:
        q, expected = case['query'], case['value']
        actual = legacy.get_client_data_history(q['ClientId'], q['TemplateId'], q['Page'], q['Limit'], q['Action'], q['Account'])
        assert actual['total'] == expected['total']
        assert [row['id'] for row in actual['rows']] == [row['id'] for row in expected['rows']]
        for old, native in zip(actual['rows'], expected['rows']):
            assert old['action'] == native['action'] and old['display_action'] == native['display_action']
            assert project(old['row_data']) == native['row_data']
            assert project(old['changed_fields']) == native['changed_fields']
        connection.rollback()

    for template in (1, 2, 3):
        book = openpyxl.load_workbook(directory / f'backup-{template}.xlsx', data_only=False)
        assert book.sheetnames == ['Data', 'Changed-Deleted History']
        current, history = sheet_rows(book['Data']), sheet_rows(book['Changed-Deleted History'])
        expected_current = query("SELECT id,row_data FROM client_data WHERE client_id=10 AND template_id=%s AND COALESCE(is_deleted,FALSE)=FALSE AND COALESCE(change_detection_status,'')<>'deleted' ORDER BY extract_date ASC NULLS LAST,source_order_upload_id ASC NULLS LAST,source_row_number ASC NULLS LAST,id", (template,))
        expected_history = query('SELECT id,action,row_data,changed_fields FROM client_data_history WHERE client_id=10 AND template_id=%s ORDER BY changed_at DESC,id DESC', (template,))
        assert [row['Record ID'] for row in current] == [row['id'] for row in expected_current]
        assert [row['History ID'] for row in history] == [row['id'] for row in expected_history]
        for row, expected in zip(current, expected_current):
            assert json.loads(row['Source row JSON'], parse_float=Decimal) == expected['row_data']
        for row, expected in zip(history, expected_history):
            assert row['Action'] == expected['action']
            assert json.loads(row['Source row JSON'], parse_float=Decimal) == expected['row_data']
            assert (json.loads(row['Changed Fields'], parse_float=Decimal) if row['Changed Fields'] else None) == expected['changed_fields']
        if template == 1:
            assert current[0]['Amount [col_3]'] == '9007199254740993.10'
            assert current[0]['Memo [col_4]'] == '=literal _x0041_ <safe>'
            assert current[0]['Balance [col_5]'] == '001 DR'
            assert any(row.get('archived_key') == 'kept' for row in history)
            assert next(row for row in history if row['History ID'] == 4)['Action'] == 'deleted'
        if template == 2:
            assert current[0]['Account Code'] == '001' and current[0]['Account Name'] == 'Current account'
        for sheet in book:
            assert all(cell.data_type != 'f' for row in sheet for cell in row)
        connection.rollback()
    large = openpyxl.load_workbook(directory / 'large.xlsx', read_only=True)
    assert len(sheet_rows(large['Data'])) == 10001
    stable = openpyxl.load_workbook(directory / 'consistent.xlsx')
    assert sheet_rows(stable['Data'])[0]['Amount [col_3]'] == '9007199254740993.10'
    assert len(sheet_rows(stable['Changed-Deleted History'])) == 7
    assert 'CONCURRENT' not in str(list(stable['Changed-Deleted History'].values))
    with zipfile.ZipFile(directory / 'backup-1.xlsx') as archive:
        xml = archive.read('xl/worksheets/sheet1.xml').decode()
        assert '_x005F_x0041_' in xml and '<f>' not in xml
        assert not any('externalLink' in name for name in archive.namelist())
    (directory / 'oracle-results.json').write_text(json.dumps({'accepted': True, 'history_cases': len(fixture['cases']), 'large_rows': 10001}, indent=2))
    print('History/OOXML source comparisons passed')
finally:
    connection.rollback()
    connection.close()
