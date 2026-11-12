"""Extract current comparison functions; synthetic scalars only, no app/config import."""
import ast
import hashlib
import json
import re
import sys
from collections import defaultdict, deque
from contextvars import ContextVar
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from pathlib import Path
from typing import Optional

import pandas as pd
from fastapi import HTTPException
from openpyxl import Workbook

root = Path(__file__).resolve().parents[2]
target = Path(sys.argv[1]).resolve()
if (root / 'dotnet-uploader/.tools').resolve() not in target.parents or target.exists():
    raise RuntimeError('Fresh owned fixture directory required')
target.mkdir(parents=True)
ns = dict(Decimal=Decimal, InvalidOperation=InvalidOperation, hashlib=hashlib, json=json, re=re,
          datetime=datetime, date=date, pd=pd, HTTPException=HTTPException, Optional=Optional,
          lru_cache=lru_cache, defaultdict=defaultdict, deque=deque, DEFAULT_DATE_ORDER='DMY',
          DEFAULT_DATE_DELIMITER='/', LEDGER_SOURCE_NOTES='Ledger Source Notes',
          UPLOAD_COMPARISON_PAGE_SIZE=1000, SOURCE_ROW_HASH_SQL="''",
          _ledger_context=ContextVar('ledger', default=None), _source_row_numbers=ContextVar('positions', default=None))

def extract(path, names):
    tree = ast.parse(path.read_text(encoding='utf-8-sig'))
    nodes = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in names
             or isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id in names for t in n.targets)]
    exec(compile(ast.Module(nodes, []), str(path), 'exec'), ns)

extract(root / 'database.py', {'resolve_document_column', 'resolve_document_date_column'})
extract(root / 'services/upload_lifecycle.py', {'declared_coverage', 'source_fingerprint', 'digest', 'match_unchanged'})
extract(root / 'routers/import_export.py', {'compare_transactions', 'extract_columns_from_row', 'find_named_column', '_source_order_pairs', '_ledger_row_number',
    '_row_fingerprint_token', '_row_comparison_fingerprint', '_numeric_comparison_value', '_exact_comparison_fingerprint',
    '_source_identity_fingerprint', '_normalized_key_value', 'LINE_KEY_ALIASES', 'get_line_key_column', '_comparison_line_key',
    'get_document_group_id', '_canonical_source_identity', '_date_text', '_date_text_for_row', '_formatting_comparison_values',
    '_comparison_text_calendar_date', '_cached_comparison_text_date', '_comparison_calendar_date', '_parse_date_text',
    '_cached_date_text', '_cacheable_date_text', 'parse_date_flexible', 'classify_date_cell', 'excel_serial_to_datetime'})
# Inputs below already use canonical scalar field values. This intentionally
# excludes coercion/derived Rates (P19/P22 gates), not the comparison algorithm.
ns['_canonical_import_row_data'] = lambda raw, *_: dict(raw)

def row(amount=100, description='Office', document='V1', line='A', account='001', debit=None, credit=0, **extra):
    return dict(Document=document, Date='03/04/2026', Amount=amount, Description=description,
                **{'Account Code': account, 'Debit': amount if debit is None else debit, 'Credit': credit, 'Line ID': line}, **extra)

specs = [
    ('repeated', [row(), row()], [row(), row()], False, True, 'replace_range'),
    ('unique_details', [row(107, 'A'), row(210, 'B')], [row(100, 'A'), row(200, 'B')], False, True, 'correction'),
    ('different_line', [row(line='B')], [row(line='A')], True, False, 'correction'),
    ('same_line', [row(107, line='A')], [row(100, line='A')], True, False, 'correction'),
    ('line_complete', [row(line='B')], [row(line='A')], True, False, 'replace_range'),
    ('ambiguous_partial', [row(11), row(21)], [row(10), row(20)], False, True, 'correction'),
    ('ambiguous_complete', [row(11), row(21)], [row(10), row(20)], False, True, 'replace_range'),
    ('account_side', [row(101, 'New', account='001'), row(201, 'New', account='002')],
                     [row(100, 'Old', account='001'), row(200, 'Old', account='002')], False, True, 'correction'),
    ('single_fallback', [row(107, 'New')], [row(100, 'Old')], False, False, 'correction'),
    ('partial_retains', [row()], [row(), row(document='V2')], False, False, 'correction'),
    ('complete_removes', [row()], [row(), row(document='V2')], False, False, 'replace_range'),
    ('empty_complete', [], [row(), row(document='V2')], False, False, 'replace_range'),
    ('blank_zero', [row(0)], [row(None, debit=0)], False, False, 'correction'),
    ('unchanged_first', [row(107), row(100)], [row(100), row(100)], False, False, 'correction'),
    ('numeric_equivalent', [row('100.000', debit='100.00')], [row()], False, True, 'correction'),
]
vectors = []
for label, incoming, current, line, account, mode in specs:
    names = [('Document', 'Text'), ('Date', 'Date'), ('Amount', 'Numeric'), ('Description', 'Text')]
    if account: names += [('Account Code', 'Text'), ('Debit', 'Numeric'), ('Credit', 'Numeric')]
    if line: names += [('Line ID', 'Text')]
    columns = [dict(id=i, column_name=name, data_type=kind, reconciliation_role='document_id' if i == 1 else None)
               for i, (name, kind) in enumerate(names, 1)]
    selected = {name for name, _ in names}
    incoming = [{k: v for k, v in r.items() if k in selected} for r in incoming]
    current = [{k: v for k, v in r.items() if k in selected} for r in current]
    stored = [dict(id=100+i, row_data=r, source_identity='keep-'+str(i), version=3,
                   document_group_id='7-'+r['Document'], source_row_number=i+2, source_order_upload_id=9,
                   extract_date='2026-04-03') for i, r in enumerate(current)]
    def query(sql, params=None, **_):
        if 'SELECT cd.id FROM client_data' in sql:
            return [dict(id=r['id']) for r in stored if r['document_group_id'] in params[2]]
        return stored
    ns['execute_query_pg'] = query
    vector = dict(label=label, incoming=incoming, current=current, columns=names, line=line, account=account, mode=mode)
    try:
        result = ns['compare_transactions'](10, 7, columns, incoming, {name: name for name in selected}, {},
            comparison_mode=mode, coverage_start='2026-04-01', coverage_end='2026-04-30', included_fields=selected)
        vector.update(ambiguous=False, unchanged=result['unchanged_count'], changed=result['changed_count'],
                      added=result['new_count'], removed=result['missing_count'],
                      changes=[dict(id=r['client_data_id'], fields=r['changed_fields']) for r in result['changed']],
                      removed_ids=[r['client_data_id'] for r in result['missing']])
    except HTTPException as error:
        if error.status_code != 409: raise
        vector.update(ambiguous=True)
    vectors.append(vector)

book = Workbook(); book.remove(book.active)
base = [['V1', '03/04/2026', 100, 'Office  supplies', '100 DR'],
        ['V1', '03/04/2026', 100, 'Office  supplies', '100 DR'], ['V2', '08/04/2026', 50, 'Other', '50 DR']]
for name, rows in [('Base', base + [['TOTAL', None, 250]]),
                   ('Changed', [[*base[0][:2], 107, *base[0][3:]], base[1], base[2], ['TOTAL', None, 257]]),
                   ('BindingReview', [base[0], base[1], ['V2', '08/04/2026', 50, 'Changed detail', '50 DR'], ['TOTAL', None, 250]]),
                   ('Empty', [['TOTAL', None, 0]]),
                   ('Ledger', [[None,None,None,'Opening Balance',None,'100 DR'],['V1','03/04/2026',10,'Payment',0,'110 DR'],
                               [None,None,None,'Invoice # 1'],[None,None,None,'Closing Balance',None,'110 DR'],['TOTAL',None,10]])]:
    ws = book.create_sheet(name)
    ws.append(['Document','Date','Amount','Memo','Credit','Balance'] if name == 'Ledger'
              else ['Document','Date','Amount','Memo','Balance','Extra'])
    for values in rows: ws.append(values)
book.save(target / 'corrections.xlsx')
(target / 'oracle.json').write_text(json.dumps(dict(vectors=vectors,
    sha256=hashlib.sha256((target / 'corrections.xlsx').read_bytes()).hexdigest()), ensure_ascii=True), encoding='utf-8')
