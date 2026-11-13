"""Owned synthetic workbooks and independent exact expected upload states."""
import json
import sys
from decimal import Decimal
from pathlib import Path

from openpyxl import Workbook


def main():
    target = Path(sys.argv[1]).resolve()
    target.mkdir(parents=True, exist_ok=False)
    headers = ['Document', 'Date', 'Amount', 'Memo', 'Balance', 'Extra']
    original = [
        ['001', '03/04/2026', '100.10', 'Repeated line', '100 DR', 'same'],
        ['001', '03/04/2026', '100.10', 'Repeated line', '100 DR', 'same'],
        ['002', '08/04/2026', '50.20', 'Other', '50 DR', 'other'],
    ]
    changed = [list(row) for row in original]
    changed[2][2] = '55.20'
    cases = {'original': original, 'repeat': original, 'correction': changed,
             'partial': [changed[2]], 'empty': [],
             'large': [['LARGE', '08/04/2026', f'{i % 91 + 1}.25', f'Line {i}', '', ''] for i in range(1000)],
             'oversize': [['LARGE', '08/04/2026', f'{i % 91 + 1}.25', f'Line {i}', '', ''] for i in range(10000)]}
    oracle = {}
    for name, rows in cases.items():
        book = Workbook()
        sheet = book.active
        sheet.title = 'Data'
        sheet.append(headers)
        for row in rows:
            values = list(row)
            values[2] = float(Decimal(values[2]))
            sheet.append(values)
        total = sum((Decimal(row[2]) for row in rows), Decimal(0))
        sheet.append(['TOTAL', None, float(total)])
        book.save(target / f'{name}.xlsx')
        oracle[name] = {'rows': len(rows), 'total': str(total), 'control_row': len(rows) + 2,
                        'documents': len({row[0] for row in rows}), 'values': rows}
    # These intentionally invalid inputs must never become successful imports.
    book = Workbook()
    book.active.append(headers)
    book.active.append(['BAD', '08/04/2026', '=1/0', 'Invalid cached formula'])
    book.active.append(['TOTAL', None, 0])
    book.save(target / 'invalid.xlsx')
    oracle['invalid'] = {'rows': 1, 'total': '0', 'control_row': 3, 'documents': 1, 'values': []}
    (target / 'oracle.json').write_text(json.dumps(oracle), encoding='utf-8')


if __name__ == '__main__':
    main()
