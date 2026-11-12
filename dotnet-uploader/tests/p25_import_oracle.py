"""Synthetic P25 workbooks and independent legacy raw-fingerprint vectors."""
import ast
import hashlib
import json
import sys
from pathlib import Path
from openpyxl import Workbook

root = Path(__file__).resolve().parents[2]
target = Path(sys.argv[1]).resolve()
owned = (root / 'dotnet-uploader' / '.tools').resolve()
if target.parent != owned or not target.name.startswith('p25-tests-') or target.exists():
    raise SystemExit('Use a fresh owned P25 fixture directory')
target.mkdir()
tree = ast.parse((root / 'routers' / 'import_export.py').read_text(encoding='utf-8'))
names = {'_row_comparison_fingerprint', '_row_fingerprint_token'}
module = ast.Module(body=[n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in names], type_ignores=[])
scope = {'hashlib': hashlib, 'json': json}
exec(compile(module, '<reviewed legacy fingerprint>', 'exec'), scope)
vectors = [
    {'a': None, 'b': 0, 'c': False, 'd': ' 001 ', 'e': True},
    {'é': ' مرحبا ', '😀': 'x', '\ue000': 'y'},
    {'a': 100, 'b': 1.25, 'c': 0.001, 'd': -12.5},
    {'a': 1e16, 'b': 1e15, 'c': 1e-5, 'd': 1e-4, 'e': -1.234567890123e20},
]
wb = Workbook()
wb.remove(wb.active)
base = [['V1', '03/04/2026', 100, 'Office  supplies', '100 DR', None],
        ['V1', '03/04/2026', 100, 'Office  supplies', '100 DR', None],
        ['V2', '08/04/2026', 50, 'Other', '50 DR', None]]
for name in ['Base', 'Changed', 'Removed', 'Info', 'Added']:
    ws = wb.create_sheet(name)
    ws.append(['Document', 'Date', 'Amount', 'Memo', 'Balance', 'Extra'])
    rows = [r[:] for r in base]
    if name == 'Changed':
        rows[1][2] = 120
    if name == 'Removed':
        rows = rows[:2]
        for row in rows:
            row[4] = 'updated'
    if name == 'Info':
        for row in rows:
            row[4] = 'updated'
    if name == 'Added':
        rows.append(['V3', '09/04/2026', 75, 'Added', '75 DR', None])
    for row in rows:
        ws.append(row)
    # Tests use a confirmed control row; adapt position to each sheet below.
    ws.append([None, None, sum(r[2] for r in rows)])
wb.save(target / 'plans.xlsx')
(target / 'oracle.json').write_text(json.dumps({'fingerprints': [
    {'values': v, 'hash': scope['_row_fingerprint_token'](v)} for v in vectors
]}), encoding='utf-8')
