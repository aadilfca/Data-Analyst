"""Differential reads against Python's existing router; owned loopback fixture only."""
import json
import sys
from decimal import Decimal
from pathlib import Path

import psycopg2
from psycopg2.extras import RealDictCursor, register_default_jsonb

directory = Path(sys.argv[1]).resolve()
fixture = json.loads((directory / 'fixture.json').read_text())
assert directory.parent.name == '.tools' and directory.name.startswith('p30-')
assert fixture['database'].startswith('auditplatform_foundation_') and fixture['user'] == 'auditplatform_test'
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from routers import data as legacy

connection = psycopg2.connect(host='127.0.0.1', port=fixture['port'], dbname=fixture['database'],
                            user=fixture['user'], password=fixture['password'], connect_timeout=3)
connection.set_session(readonly=True, isolation_level='REPEATABLE READ')
register_default_jsonb(connection, loads=lambda value: json.loads(value, parse_float=Decimal))

def query(sql, params=None, **kwargs):
    with connection.cursor(cursor_factory=RealDictCursor) as cursor:
        cursor.execute(sql, params)
        return [dict(row) for row in cursor.fetchall()]

def snapshot(callback):
    try:
        return callback(connection)
    finally:
        connection.rollback()

legacy.execute_query_pg = query
legacy.database.execute_query_pg = query
legacy.database.run_read_snapshot_pg = snapshot
legacy.attach_history_flags = lambda rows: None  # Separate P31/audit work, not a read oracle.
legacy.attach_assignment_flags = lambda rows: None
labels = {'col_1': 'Document', 'col_2': 'Date', 'col_3': 'Amount', 'col_4': 'Memo', 'col_5': 'Balance'}
checked = []
try:
    for case in fixture['expected']:
        q, expected = case['query'], case['value']
        result = legacy.get_preview_rows(client_id=q['ClientId'], template_id=q['TemplateId'],
            date_start=q['Start'], date_end=q['End'], page=q['Page'], limit=q['Limit'], search=q['Search'],
            amount_min=q['Minimum'], amount_max=q['Maximum'], sunday_only=q['Sunday'], data_quality=q['Quality'],
            document_id=None, ledger_account_code=q['Account'], include_metrics=q['Metrics'], perf=False)
        assert result['total_rows'] == expected['total_rows'], (q, result)
        assert result['sunday_count'] == expected['sunday_count']
        assert [r['id'] for r in result['rows']] == [r['id'] for r in expected['rows']]
        actual_metrics, native_metrics = result['preview_metrics'], expected['preview_metrics']
        assert actual_metrics['status'] == native_metrics['status']
        if actual_metrics['status'] == 'ok':
            for key in ['row_count', 'document_count', 'missing_document_rows', 'footing_configured']:
                assert actual_metrics[key] == native_metrics[key], (q, key)
            totals = {labels.get(key, key): Decimal(value) for key, value in actual_metrics['footing_totals'].items()}
            assert totals == {key: Decimal(value) for key, value in native_metrics['footing_totals'].items()}
        checked.append({'template': q['TemplateId'], 'page': q['Page'], 'rows': result['total_rows']})
    (directory / 'oracle-results.json').write_text(json.dumps({'accepted': True, 'cases': checked}, indent=2))
    print(f'{len(checked)} Python/native SQL preview comparisons passed')
finally:
    connection.rollback()
    connection.close()
