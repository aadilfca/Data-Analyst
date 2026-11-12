"""Owned synthetic workbooks and AST-extracted Python derivation oracles."""
import ast
from collections import defaultdict
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_EVEN, getcontext
import hashlib
import json
from pathlib import Path
import re
import sys
from typing import Any, Iterable, Mapping
from openpyxl import Workbook

root = Path(__file__).resolve().parents[2]
target = Path(sys.argv[1]).resolve()
if target.parent != (root / 'dotnet-uploader' / '.tools').resolve() or not target.name.startswith('p26-tests-') or target.exists():
    raise SystemExit('Use a fresh owned P26 fixture directory')
target.mkdir()


def extracted(path, names, assignments=False):
    tree = ast.parse((root / path).read_text(encoding='utf-8'))
    nodes = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in names
             or assignments and isinstance(n, (ast.Assign, ast.AnnAssign))]
    module = ast.Module(body=[ast.ImportFrom(module='__future__', names=[ast.alias(name='annotations')], level=0), *nodes], type_ignores=[])
    scope = dict(globals())
    exec(compile(ast.fix_missing_locations(module), '<reviewed Python derivation>', 'exec'), scope)
    return scope


rate_scope = extracted('routers/import_export.py', {'find_named_column', 'apply_rate_derivation'})
columns = [{'column_name': name} for name in ['Rate', 'Quantity', 'Amount']]
getcontext().prec = 1000
rates = []
for amount, quantity, precision in [('100', '3', 2), ('10', '0', 2), ('', '2', 2), ('-5', '2', 0),
                                    ('2.675', '1', 2), ('100', '7', 4), ('100000000000000000000000000000', '2', 0), ('0.5', '1', 0)]:
    row = {'Amount': amount, 'Quantity': quantity}
    rate_scope['apply_rate_derivation'](row, columns, 'derived', precision)
    actual = row.get('Rate')
    exact = None if not Decimal(quantity) else (Decimal(amount or '0') / Decimal(quantity)).quantize(Decimal(1).scaleb(-precision), rounding=ROUND_HALF_EVEN)
    rates.append(dict(amount=amount, quantity=quantity, precision=precision, expected=actual,
                      review=actual is not None and Decimal.from_float(actual).quantize(Decimal(1).scaleb(-precision), rounding=ROUND_HALF_EVEN) != exact))
classification = extracted('services/cash_bank_classification.py', {
    '_normalise_label', '_text', '_canonical_code', '_decode_row_data', '_field_value', '_parse_amount', '_parse_date',
    '_transaction_period_keys', 'build_classification_row'}, assignments=True)
canonical = extracted('services/cashflow_voucher_canonicalization.py', {
    '_text', 'normalize_voucher_id', '_normalize_account_code', '_date_text', '_amount_text', 'voucher_fingerprint', '_source_priority', 'resolve_voucher_occurrences'}, assignments=True)
projections, lines = [], []
for code, name, debit, credit, day in [('001', 'Cash', 100, 0, '2026-04-14'), ('002', 'Sales', 0, 100, '2026-04-15')]:
    raw = {'Voucher ID': 'V1', 'Account Detail': code, 'Account Name': name, 'Debit Amount': debit, 'Credit Amount': credit, 'Transaction Date': day}
    expected = classification['build_classification_row']({'id': 1, 'client_id': 10, 'template_id': 1, 'upload_id': 1, 'row_data': raw}, {'001': {'cash'}}, 1)
    projections.append(dict(native=dict(a='V1', b=day, c=code, d=name, e=debit, f=credit), date=day, expected=expected))
    lines.append(expected)

wb = Workbook()
wb.remove(wb.active)
base = [['V1', '03/04/2026', 100, '001', 0, 'Cash', 'Cash leg'],
        ['V1', '03/04/2026', 100, '001', 0, 'Cash', 'Cash leg'],
        ['V1', '03/04/2026', 0, '002', 200, 'Sales', 'Credit'],
        ['V2', '15/04/2026', 50, '001', 0, 'Cash', 'Cash leg'],
        ['V2', '15/04/2026', 0, '002', 50, 'Sales', 'Credit']]
for name in ['Base', 'Changed', 'Partial', 'Incomplete', 'Removed']:
    ws = wb.create_sheet(name)
    ws.append(['Document', 'Date', 'Amount', 'Account', 'Credit', 'Name', 'Memo'])
    rows = [r[:] for r in base]
    if name == 'Changed':
        rows[0][2], rows[2][4] = 120, 220
    if name in ('Partial', 'Removed'):
        rows = rows[:3]
        for row in rows:
            row[6] = 'Updated memo'
    if name == 'Incomplete':
        rows = [rows[0], rows[2]]
    for row in rows:
        ws.append(row)
    ws.append([None, None, sum(r[2] for r in rows)])
wb.save(target / 'plans.xlsx')
rate_book = Workbook()
ws = rate_book.active
ws.title = 'Rates'
ws.append(['Document', 'Date', 'Amount', 'Quantity', 'Rate', 'Memo'])
ws.append(['R1', '03/04/2026', 10, 3, '001.234567890123456789', 'source'])
ws.append(['R2', '03/04/2026', 20, 0, None, 'zero quantity'])
ws.append([None, None, 30])
rate_book.save(target / 'rates.xlsx')
decisions = []
for group, sequences, fingerprints in [('V', ['2026-01-01', '2026-01-02', '2026-01-03'], ['same', 'same', 'changed']),
                                       ('N', ['9', '10'], ['same', 'same'])]:
    for index, (sequence, fingerprint) in enumerate(zip(sequences, fingerprints), 1):
        decisions.append(dict(client_id=10, template_id=1, upload_id=index, voucher_identity=group, voucher_fingerprint=fingerprint,
                              occurrence_key=f'upload:{index}:voucher:{group}', source_sequence=sequence))
resolved = canonical['resolve_voucher_occurrences'](decisions)
(target / 'oracle.json').write_text(json.dumps(dict(rates=rates, projections=projections, decisions=resolved,
    fingerprint=canonical['voucher_fingerprint'](lines)), ensure_ascii=True), encoding='utf-8')
