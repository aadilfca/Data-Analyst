"""Read workbook shapes without editing client sources or loading the application."""
from pathlib import Path
import json
import re

import openpyxl

ROOT = Path(__file__).resolve().parents[2]


def inventory():
    result = []
    for path in sorted((ROOT / 'uploads').glob('*.xlsx')):
        if re.match(r'^[a-f0-9]{32}_', path.name):
            if not any(word in path.name.lower() for word in ('ledger', 'cash in hand', 'cashbook', 'bank', 'hmb')):
                continue
        try:
            book = openpyxl.load_workbook(path, read_only=True, data_only=True)
            sheets = []
            for sheet in book:
                sheets.append({'name': sheet.title, 'rows': sheet.max_row,
                               'columns': sheet.max_column,
                               'head': list(sheet.iter_rows(min_row=1, max_row=2, values_only=True))})
            book.close()
            result.append({'file': path.name, 'sheets': sheets})
        except Exception as error:
            result.append({'file': path.name, 'error': str(error)})
    target = Path(__file__).parent / 'inventory.json'
    target.write_text(json.dumps(result, default=str, indent=2), encoding='utf-8')
    for item in result:
        print(json.dumps(item, default=str))


if __name__ == '__main__':
    inventory()
