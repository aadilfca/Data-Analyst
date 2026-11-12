"""Synthetic identity/matching vectors extracted without importing the application."""
import ast
import hashlib
import json
import re
import sys
from collections import defaultdict, deque
from contextvars import ContextVar
from datetime import datetime, date
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
    raise RuntimeError('A fresh owned fixture path is required')
target.mkdir(parents=True)
ns = dict(Decimal=Decimal, InvalidOperation=InvalidOperation, hashlib=hashlib, json=json, re=re, datetime=datetime,
          date=date, pd=pd, HTTPException=HTTPException, Optional=Optional, lru_cache=lru_cache,
          DEFAULT_DATE_ORDER='DMY', DEFAULT_DATE_DELIMITER='/', _ledger_context=ContextVar('ledger', default=None),
          defaultdict=defaultdict, deque=deque)
def extract(path, names):
    tree = ast.parse(path.read_text(encoding='utf-8-sig'))
    nodes = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in names
             or isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id in names for t in n.targets)]
    exec(compile(ast.Module(nodes, []), str(path), 'exec'), ns)
extract(root / 'database.py', {'resolve_document_column'})
extract(root / 'routers/import_export.py', {'_row_comparison_fingerprint', '_numeric_comparison_value',
    '_exact_comparison_fingerprint', '_source_identity_fingerprint', '_normalized_key_value', 'LINE_KEY_ALIASES',
    'get_line_key_column', '_comparison_line_key', 'get_document_group_id', '_canonical_source_identity', '_canonical_source_identities',
    '_comparison_text_calendar_date', '_cached_comparison_text_date', '_comparison_calendar_date', '_formatting_comparison_values',
    '_parse_date_text', '_cached_date_text', '_cacheable_date_text', 'parse_date_flexible', 'classify_date_cell', 'excel_serial_to_datetime'})
extract(root / 'services/upload_lifecycle.py', {'match_unchanged'})
columns = [dict(id=i, column_name=name, data_type=kind, reconciliation_role='document_id' if i == 1 else None)
           for i, (name, kind) in enumerate([('Document', 'Text'), ('Date', 'Date'), ('Amount', 'Numeric'), ('Description', 'Text')], 1)]
identity = []
for label, row, occurrence, account, line, review in [
    ('integer', {'Document':'V1', 'Amount':100}, 1, None, False, False),
    ('scale', {'Document':'V1', 'Amount':'100.000'}, 1, None, False, False),
    ('blank', {'Document':'V1', 'Amount':None}, 1, None, False, False),
    ('zero', {'Document':'V1', 'Amount':0}, 1, None, False, False),
    ('negative_zero', {'Document':'V1', 'Amount':'-0.000'}, 1, None, False, False),
    ('unicode', {'Document':'آڈٹ-🙂', 'Amount':'١_٢.٥٠', 'Description':'Café\tA\nB'}, 1, None, False, False),
    ('escapes', {'Document':'"\\V\x7f', 'Description':''.join(chr(i) for i in range(1, 32))+'<>&', 'Amount':'.000001'}, 1, None, False, False),
    ('scientific', {'Document':'V1', 'Amount':'1.234e-7'}, 1, None, False, False),
    ('large_exact', {'Document':'V1', 'Amount':'1234567890123456789012345678'}, 1, None, False, False),
    ('precision_review', {'Document':'V1', 'Amount':'12345678901234567890123456789.1'}, 1, None, False, True),
    ('repeated', {'Document':'V1', 'Amount':100}, 2, None, False, False),
    ('ledger', {'Document':'V1', 'Amount':100, 'Ledger Source Notes':'Invoice A\nInvoice B'}, 1, '001-A', False, False),
    ('ledger_numeric_text', {'Document':'V1', 'Amount':'1,000.00'}, 1, '001-A', False, False),
    ('decimal_underscores', {'Document':'V1', 'Amount':'_100_'}, 1, None, False, False),
    ('line', {'Document':'V1', 'Line ID':' AbC-001 ', 'Amount':100}, 1, None, True, False),
    ('line_unicode_review', {'Document':'V1', 'Line ID':'Straße', 'Amount':100}, 1, None, True, True),
    ('unicode_order', {'Document':'V1', '\ue000':'A', '\U00010000':'B', 'Amount':1}, 1, None, False, False),
]:
    selected = columns + ([dict(id=5, column_name='Line ID', data_type='Text')] if line else [])
    token = ns['_ledger_context'].set({'account_code':account} if account else None)
    try:
        expected = ns['_canonical_source_identity'](selected, row, 7, occurrence_index=occurrence)
    finally:
        ns['_ledger_context'].reset(token)
    identity.append(dict(label=label, row=row, occurrence=occurrence, account=account, line=line, review=review, expected=expected))

def row(text='Office  supplies', amount=100, day='03/04/2026', balance='100 DR'):
    return {'Document':'V1', 'Date':day, 'Amount':amount, 'Description':text, 'Balance':balance}
comparisons = []
for label, uploaded, current, primary_old, primary_new in [
    ('exact_priority', [row('Office   supplies'), row('Office supplies')], [row('Office  supplies'), row('Office supplies')], '2026-04-03', '2026-04-03'),
    ('multiplicity', [row(), row()], [row(), row()], '2026-04-03', '2026-04-03'),
    ('numeric', [row(amount='100.000')], [row(amount=100)], '2026-04-03', '2026-04-03'),
    ('blank_zero', [row(amount=0)], [row(amount=None)], '2026-04-03', '2026-04-03'),
    ('date_format', [row(day='2026-04-03')], [row(day='03/04/2026')], '2026-04-03', '2026-04-03'),
    ('date_meaning', [row()], [row()], '2026-03-04', '2026-04-03'),
    ('line_break', [row('Office supplies Tag: A')], [row('Office supplies\nTag: A')], '2026-04-03', '2026-04-03'),
    ('unchecked', [row(balance='80 DR')], [row()], '2026-04-03', '2026-04-03'),
]:
    def exact(r, stored):
        return ns['_exact_comparison_fingerprint'](r, columns, {c['column_name'] for c in columns}), primary_old if stored else primary_new
    def equivalent(r, stored):
        return tuple(sorted(ns['_formatting_comparison_values'](r, columns, '/', 'DMY',
            primary_date=primary_old if stored else primary_new, stored=stored,
            included_fields={c['column_name'] for c in columns}).items()))
    matched, new, old = ns['match_unchanged'](uploaded, current, exact, equivalent)
    pairs = sorted((next(i for i, r in enumerate(uploaded) if r is a), next(i for i, r in enumerate(current) if r is b)) for a, b in matched)
    comparisons.append(dict(label=label, uploaded=uploaded, current=current, old_date=primary_old, new_date=primary_new,
                            pairs=pairs, unmatched_new=len(new), unmatched_old=len(old)))

book = Workbook();book.remove(book.active)
headers = ['Document','Date','Amount','Memo','Balance','Line ID']
base = [['V1','03/04/2026',100,'Office  supplies','100 DR'], ['V1','03/04/2026',100,'Office  supplies','100 DR'],
        ['V2','08/04/2026',50,'Other','50 DR']]
def sheet(name, rows, header=None):
    ws = book.create_sheet(name);ws.append(header or headers)
    for value in rows: ws.append(value)
sheet('Base', base + [['TOTAL',None,250]])
sheet('Reordered', [base[2],base[0],base[1]] + [['TOTAL',None,250]])
sheet('Changed', [[*base[0][:2],107,*base[0][3:]],base[1],base[2],['TOTAL',None,257]])
sheet('Unchecked', [[*base[0][:4],'80 DR'],base[1],base[2],['TOTAL',None,250]])
sheet('Variance', base + [['TOTAL',None,251]])
sheet('Ledger', [[None,None,None,'Opening Balance',None,'100 DR'],['V1','03/04/2026',10,'Payment',0,'110 DR'],
                 [None,None,None,'Invoice # 1'],[None,None,None,'Closing Balance',None,'110 DR'],['TOTAL',None,10]],
      ['Document','Date','Amount','Memo','Credit','Balance'])
sheet('Native', [['V1',datetime(2026,4,3),100,'Office  supplies','100 DR'],base[1],base[2],['TOTAL',None,250]])
book.save(target / 'identity.xlsx');book.close()
(target / 'oracle.json').write_text(json.dumps(dict(identity=identity, comparisons=comparisons,
    sha256=hashlib.sha256((target / 'identity.xlsx').read_bytes()).hexdigest()), ensure_ascii=True), encoding='utf-8')
