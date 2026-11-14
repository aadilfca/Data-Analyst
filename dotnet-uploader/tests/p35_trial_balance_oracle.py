"""Synthetic P35 fixtures and independent canonical/Python storage checks."""
import json
import sys
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))


def generate(directory):
    from openpyxl import Workbook
    headers = ['Code', 'Name', 'Opening Dr', 'Opening Cr', 'Period Dr', 'Period Cr', 'Closing Dr', 'Closing Cr', 'Type', 'Unmapped']
    rows = [
        ['001', 'Parent', 100, 0, 0, 0, 100, 0, 'Master', 'preview only'],
        ['001-01', '=literal leaf', None, None, 10, 0, 13, 0, 'Detail', 'not stored'],
        [None, None, None, None, None, None, None, None, None, None],
        ['002', 'Credit', 0, 0, 0, 5, 0, 5, 'Detail', 'not stored'],
        [None, 'Excluded subtotal', 999, 0, 0, 0, 999, 0, None, None],
        [None, 'ERP total', 0, 0, 10, 5, 13, 5, None, None],
    ]
    for name in ['base', 'changed', 'real_precision', 'fraction_precision', 'numeric_code', 'typed_error']:
        book = Workbook(); sheet = book.active; sheet.append(headers)
        for row in rows: sheet.append(row)
        sheet.cell(3, 2).data_type = 's'  # Literal account title, never a formula.
        if name == 'real_precision': sheet.cell(3, 7, 16777217)
        if name == 'fraction_precision': sheet.cell(3, 7, '0.123456789')
        if name == 'numeric_code': sheet.cell(5, 1, 2.0)
        if name == 'changed': sheet.cell(5, 2, 'Changed title')
        if name == 'typed_error': sheet.cell(2, 9, '#N/A').data_type = 'e'
        book.save(directory / (name + '.xlsx'))
    book = Workbook(); sheet = book.active; sheet.append(headers)
    for n in range(10000): sheet.append([f'AC{n:06}', f'Account {n}', 10, 10, 2, 2, 12, 12, 'Detail', 'preview only'])
    book.save(directory / 'large.xlsx')


def verify(directory):
    import psycopg2
    from psycopg2.extras import RealDictCursor
    import database
    database.DB_NAME = str(directory / 'unused-oracle.db')
    from routers import trial_balance as tb
    fixture = json.loads((directory / 'fixture.json').read_text())
    assert fixture['host'] == '127.0.0.1'
    assert fixture['database'].startswith('auditplatform_foundation_')
    from openpyxl import load_workbook
    book = load_workbook(directory / 'base.xlsx', data_only=True)
    values = list(book.active.values); headers = values[0]
    mapping = fixture['mapping']; overrides = {5: '003'}
    expected = tb.validate_trial_balance_contents((directory / 'base.xlsx').read_bytes(), 'base.xlsx', mapping,
        code_overrides=overrides, excluded_excel_rows=[6], total_excel_row=7)
    assert expected['valid'], expected['errors']
    with psycopg2.connect(host='127.0.0.1', port=fixture['port'], dbname=fixture['database'], user=fixture['user'], password=fixture['password']) as connection:
        with connection.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute('SET TRANSACTION READ ONLY')
            cur.execute('SELECT * FROM trial_balance_accounts WHERE upload_id=%s ORDER BY sort_order', (fixture['upload'],))
            actual = cur.fetchall(); assert len(actual) == 3
            roles = ['opening_dr', 'opening_cr', 'period_dr', 'period_cr', 'closing_dr', 'closing_cr']
            expected_codes = ['001', '001-01', '003']
            for row, excel_row, code in zip(actual, [2, 3, 5], expected_codes):
                source = dict(zip(headers, values[excel_row-1]))
                assert row['account_code'] == code
                assert row['source_account_code'] == source['Code']
                assert row['sort_order'] == excel_row-2
                assert row['account_name'] == source['Name']
                assert row['source_account_type'] == source['Type']
                assert row['classification'] == ('parent' if code == '001' else 'leaf')
                assert row['parent_account_code'] == ('001' if code == '001-01' else None)
                assert row['raw_row_data'] == {header: source[header] for header in mapping.values() if header}
                for role in roles:
                    original = source[mapping[role + '_column']]
                    stored = row[role]
                    assert (original is None) == (stored is None)
                    if stored is not None: assert Decimal(str(stored)) == Decimal(str(original))
            cur.execute('SELECT account_id,source_excel_row,expected_closing_net,original_closing_net,difference_net,closing_dr_adjustment,closing_cr_adjustment FROM trial_balance_rollforward_adjustments WHERE upload_id=%s', (fixture['upload'],))
            differences = cur.fetchall(); assert len(differences) == len(expected['rollforward_differences']) == 1
            difference = differences[0]; original = expected['rollforward_differences'][0]
            assert difference['source_excel_row'] == original['excel_row']
            for key in ['expected_closing_net', 'original_closing_net', 'difference_net', 'closing_dr_adjustment', 'closing_cr_adjustment']:
                assert difference[key] == Decimal(original[key + '_exact'])
            cur.execute('SELECT validation_controls FROM trial_balance_uploads WHERE id=%s', (fixture['upload'],))
            controls = cur.fetchone()['validation_controls']
            if isinstance(controls, str): controls = json.loads(controls)
            for key in ['account_classifications', 'source_total_rows', 'global_adjustments_automatic', 'erp_unbalanced', 'no_total_row', 'total_excel_row']:
                expected_value = [list(pair) for pair in expected[key]] if key == 'account_classifications' else expected[key]
                assert controls[key] == expected_value, key
            for key in ['original_totals', 'adjustment_totals', 'adjusted_totals']:
                assert {k: Decimal(v) for k, v in controls['draft_reconciliation'][key].items()} == {k: Decimal(v) for k, v in expected['draft_reconciliation'][key].items()}
            footing, reason = tb._saved_trial_balance_reconciliation([r for r in actual if r['classification'] == 'leaf'], differences, controls['global_adjustments'])
            assert reason is None and footing is not None
            for role, value in footing['adjusted_totals'].items(): assert Decimal(value) == Decimal(expected['draft_reconciliation']['adjusted_totals'][role])
            cur.execute('SELECT count(*) n FROM audit_log WHERE operation_id=%s', (fixture['operation'],)); assert cur.fetchone()['n'] == 1
    (directory / 'oracle-result.json').write_text(json.dumps({'passed': True, 'accounts': 3, 'differences': 1, 'source_and_python_parity': True}))


if __name__ == '__main__':
    directory = Path(sys.argv[1]).resolve()
    assert directory.is_relative_to(ROOT / '.tools') and directory.name.startswith('p35-')
    directory.mkdir(parents=True, exist_ok=True)
    generate(directory) if sys.argv[2] == 'generate' else verify(directory)
    print('P35 synthetic ' + sys.argv[2] + ' passed')
