"""Owned synthetic coverage oracle. Extract pure legacy helpers; never boot the app."""
import ast
import hashlib
import json
import re
import sys
from contextvars import ContextVar
from datetime import date, datetime
from functools import lru_cache
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
from fastapi import HTTPException
from openpyxl import Workbook

root = Path(__file__).resolve().parents[2]
target = Path(sys.argv[1]).resolve()
tools = root / 'dotnet-uploader' / '.tools'
if tools.resolve() not in target.parents or target.exists():
    raise RuntimeError('A fresh owned test directory is required')
target.mkdir(parents=True)
ns = dict(datetime=datetime, date=date, pd=pd, lru_cache=lru_cache, HTTPException=HTTPException,
          DEFAULT_DATE_DELIMITER='/', DEFAULT_DATE_ORDER='DMY', _ledger_context=ContextVar('ledger', default=None))
ns['resolve_document_date_column'] = lambda columns: next((c for c in columns if c.get('reconciliation_role') == 'document_date'), None)
names = {'_parse_date_text', '_cached_date_text', '_cacheable_date_text', 'parse_date_flexible',
         'classify_date_cell', 'excel_serial_to_datetime', '_parse_date_cell', '_validate_complete_report_inputs',
         '_assert_incoming_dates_within_coverage', '_suggest_document_date_range'}
tree = ast.parse((root / 'routers/import_export.py').read_text(encoding='utf-8-sig'))
exec(compile(ast.Module([n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in names], []), 'legacy-coverage', 'exec'), ns)
lifecycle = ast.parse((root / 'services/upload_lifecycle.py').read_text(encoding='utf-8-sig'))
exec(compile(ast.Module([n for n in lifecycle.body if isinstance(n, ast.FunctionDef) and n.name == 'declared_coverage'], []), 'legacy-period', 'exec'), ns)
columns = [dict(id=2, column_name='Date', data_type='Date', reconciliation_role='document_date')]
vectors = []
for label, start, end, confirmed, mapped, role, values, order, delimiter in [
    ('Full', '2026-04-01', '2026-04-30', True, True, True, ['03/04/2026', '08/04/2026'], 'DMY', '/'),
    ('SingleDay', '2026-04-03', '2026-04-03', True, True, True, ['03/04/2026'], 'DMY', '/'),
    ('Outside', '2026-04-04', '2026-04-30', True, True, True, ['03/04/2026', '08/04/2026'], 'DMY', '/'),
    ('Reversed', '2026-04-30', '2026-04-01', True, True, True, ['03/04/2026'], 'DMY', '/'),
    ('Unconfirmed', '2026-04-01', '2026-04-30', False, True, True, ['03/04/2026'], 'DMY', '/'),
    ('MissingPeriod', None, None, True, True, True, ['03/04/2026'], 'DMY', '/'),
    ('MissingMap', '2026-04-01', '2026-04-30', True, False, True, ['03/04/2026'], 'DMY', '/'),
    ('MissingRole', '2026-04-01', '2026-04-30', True, True, False, ['03/04/2026'], 'DMY', '/'),
    ('MonthFirst', '2026-03-01', '2026-08-31', True, True, True, ['03/04/2026', '08/04/2026'], 'MDY', '/'),
    ('Hyphen', '2026-04-01', '2026-04-30', True, True, True, ['03-04-2026', '08-04-2026'], 'DMY', '-'),
    ('Native', '2026-04-01', '2026-04-30', True, True, True, [datetime(2026, 4, 3, 23, 59), datetime(2026, 4, 8)], 'MDY', '/'),
    ('Serial', '2026-04-01', '2026-04-30', True, True, True, [46115, 46120], 'DMY', '/'),
]:
    selected_columns = columns if role else [{**columns[0], 'reconciliation_role': None}]
    mapping = {'Date': 'Date'} if mapped else {}
    request = SimpleNamespace(comparison_mode='replace_range', coverage_start=start, coverage_end=end, confirm_report_coverage=confirmed)
    records = [{'Date': value} for value in values]
    passed = True
    try:
        scope = ns['_validate_complete_report_inputs'](request, selected_columns, mapping)
        ns['declared_coverage'](start, end)
        ns['_assert_incoming_dates_within_coverage'](records, scope, mapping, delimiter, order)
    except HTTPException:
        passed = False
    suggestion = ns['_suggest_document_date_range'](records, 'Date', 'Date', delimiter, order)
    vectors.append(dict(label=label, start=start, end=end, confirmed=confirmed, mapped=mapped, role=role,
                        order=order, delimiter=delimiter, passed=passed, suggestion=suggestion))

book = Workbook()
book.remove(book.active)
headers = ['Document', 'Date', 'Amount', 'Description', 'Credit', 'Balance']
def sheet(name, rows):
    ws = book.create_sheet(name)
    ws.append(headers)
    for row in rows:
        ws.append(row)
base = [['A', '03/04/2026', .1], ['B', '08/04/2026', .2]]
sheet('Base', base + [['TOTAL', '01/01/1900', .3]])
sheet('Variance', base + [['TOTAL', '01/01/1900', .31]])
sheet('SingleDay', [base[0]])
sheet('Hyphen', [['A', '03-04-2026', .1], ['B', '08-04-2026', .2]])
sheet('Native', [['A', datetime(2026, 4, 3, 23, 59), .1], ['B', datetime(2026, 4, 8), .2]])
sheet('Serial', [['A', 46115, .1], ['B', 46120, .2]])
sheet('Empty', [])
sheet('EmptyControl', [['TOTAL', None, 0]])
sheet('BadEmptyControl', [['TOTAL', None, 3]])
sheet('Rejected', [[None, '03/04/2026', .1]])
sheet('BadDate', [['A', 'not a date', .1]])
sheet('Repeated', base + [base[0]])
sheet('Outside', [['A', '31/03/2026', .1], base[1]])
sheet('Ledger', [[None, None, None, 'Opening balance', None, '100 DR'],
                 ['A', '03/04/2026', .1, 'Payment', 0, '100.1 DR'],
                 [None, None, None, 'Invoice detail'],
                 [None, None, None, 'Closing balance', None, '100.1 DR'],
                 ['TOTAL', '01/01/1900', .1]])
sheet('LedgerOutside', [['A', '31/03/2026', .1, 'Payment', 0, '100.1 DR']])
book.save(target / 'coverage.xlsx')
book.close()
(target / 'oracle.json').write_text(json.dumps(dict(vectors=vectors,
    sha256=hashlib.sha256((target / 'coverage.xlsx').read_bytes()).hexdigest()), default=str), encoding='utf-8')
