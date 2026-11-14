"""Owned synthetic fixtures and independent P36 stored-value/footing checks."""
import json
import sys
from decimal import Decimal
from pathlib import Path
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))


def generate(directory):
    from openpyxl import Workbook
    headers = ['Code', 'Name', 'Opening Dr', 'Opening Cr', 'Period Dr', 'Period Cr', 'Closing Dr', 'Closing Cr', 'Type', 'Unmapped']
    rows = [['001', 'Parent', 100, 0, 0, 0, 100, 0, 'Master', 'preview'],
            ['001-01', '=literal leaf', None, None, 10, 0, 13, 0, 'Detail', 'preview'],
            [None] * 10, ['002', 'Credit', 0, 0, 0, 5, 0, 5, 'Detail', 'preview'],
            [None, 'Excluded subtotal', 999, 0, 0, 0, 999, 0, None, None],
            [None, 'ERP total', 0, 0, 10, 5, 13, 5, None, None]]
    book = Workbook(); sheet = book.active; sheet.append(headers)
    for row in rows: sheet.append(row)
    sheet.cell(3, 2).data_type = 's'; book.save(directory / 'base.xlsx')
    book = Workbook(); sheet = book.active; sheet.append(headers)
    for n in range(10000): sheet.append([f'AC{n:06}', f'Account {n}', 10, 10, 2, 2, 12, 12, 'Detail', 'preview'])
    book.save(directory / 'large.xlsx')


def verify(directory):
    import psycopg2
    from psycopg2.extras import RealDictCursor
    import database
    database.DB_NAME = str(directory / 'unused-oracle.db')
    from routers import trial_balance as tb
    f = json.loads((directory / 'fixture.json').read_text())
    assert f['host'] == '127.0.0.1' and f['database'].startswith('auditplatform_foundation_')
    with psycopg2.connect(host=f['host'], port=f['port'], dbname=f['database'], user=f['user'], password=f['password']) as connection:
        with connection.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute('SET TRANSACTION READ ONLY')
            cur.execute("""SELECT md5(COALESCE((SELECT jsonb_agg(to_jsonb(a)-ARRAY['account_code','parent_account_code','level','classification','manually_overridden'] ORDER BY id)::text FROM trial_balance_accounts a WHERE upload_id=%s),'')||COALESCE((SELECT jsonb_agg(to_jsonb(x) ORDER BY id)::text FROM trial_balance_rollforward_adjustments x WHERE upload_id=%s),'')||(SELECT to_jsonb(u)::text FROM trial_balance_uploads u WHERE id=%s)) hash""", (f['upload'],) * 3)
            assert cur.fetchone()['hash'] == f['source_before']
            cur.execute('SELECT * FROM trial_balance_accounts WHERE upload_id=%s ORDER BY sort_order', (f['upload'],)); rows = cur.fetchall()
            assert [r['account_code'] for r in rows] == ['010', '010-01', '003']
            assert [r['source_account_code'] for r in rows] == ['001', '001-01', '002']
            assert rows[1]['raw_row_data']['Code'] == '001-01' and rows[1]['opening_dr'] is None
            assert rows[1]['closing_dr'] == 13 and rows[1]['manually_overridden'] and rows[1]['classification'] == 'parent'
            hierarchy = tb.build_account_code_hierarchy(r['account_code'] for r in rows)
            leaves = [r for r in rows if (r['classification'] if r['manually_overridden'] else hierarchy['classification'][r['account_code']]) == 'leaf']
            cur.execute('SELECT * FROM trial_balance_rollforward_adjustments WHERE upload_id=%s', (f['upload'],)); adjustments = cur.fetchall()
            cur.execute('SELECT validation_controls FROM trial_balance_uploads WHERE id=%s', (f['upload'],)); controls = json.loads(cur.fetchone()['validation_controls'])
            expected, error = tb._saved_trial_balance_reconciliation(leaves, adjustments, controls['global_adjustments']); assert error is None
            cur.execute("SELECT count(*) n FROM platform_trial_balance_review_operations WHERE client_id=10 AND state='completed'"); assert cur.fetchone()['n'] == 2
            cur.execute("SELECT count(*) n FROM audit_log WHERE action='TRIAL_BALANCE_REVIEW'"); assert cur.fetchone()['n'] == 2
    headers = {'Cookie': 'session_token=' + 'a' * 64}
    with urlopen(Request(f['address'] + '/api/auth/tenants', headers=headers)) as response: context = json.load(response)
    headers['X-App-Context'] = context['context_key']
    with urlopen(Request(f['address'] + f"/api/trial-balance/accounts?client_id=10&upload_id={f['upload']}&limit=1", headers=headers)) as response: page = json.load(response)
    actual = page['validation_controls']['current_reconciliation']; assert page['validation_controls']['stale']
    for group in ['original_totals', 'adjustment_totals', 'adjusted_totals']:
        assert {k: Decimal(v) for k, v in actual[group].items()} == {k: Decimal(v) for k, v in expected[group].items()}
    assert page['pagination']['total'] == 3 and len(page['accounts']) == 1
    (directory / 'oracle-result.json').write_text(json.dumps({'passed': True, 'source_unchanged': True, 'python_footing_parity': True}))


if __name__ == '__main__':
    directory = Path(sys.argv[1]).resolve()
    assert directory.is_relative_to(ROOT / '.tools') and directory.name.startswith('p36-')
    directory.mkdir(parents=True, exist_ok=True)
    generate(directory) if sys.argv[2] == 'generate' else verify(directory)
    print('P36 synthetic ' + sys.argv[2] + ' passed')
