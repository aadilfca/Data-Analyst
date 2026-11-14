"""Owned synthetic P34 source/legacy oracle; no client files or database calls."""
import hashlib
import json
import os
import struct
import sys
from datetime import datetime
from pathlib import Path
from io import BytesIO
from unittest.mock import patch
from zipfile import ZipFile, ZIP_DEFLATED
from xml.etree import ElementTree as ET

import openpyxl

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
os.environ['DATABASE_URL'] = 'postgresql://owned-unused'
from routers import trial_balance as legacy

ROLES = legacy.REQUIRED_MAPPING_ROLES
HEADERS = legacy.STANDARD_TEMPLATE_HEADERS
MAPPING = dict(zip(ROLES, HEADERS))


def xls(path, matrix):
    """Original minimal BIFF8/CFB, following the accepted P17 fixture pattern."""
    def rec(code, data=b''):
        return struct.pack('<HH', code, len(data)) + data
    def bof(kind):
        return rec(0x0809, struct.pack('<HHHHII', 0x0600, kind, 0x0DBB, 1997, 0x41, 6))
    def label(row, col, value):
        return rec(0x0204, struct.pack('<HHHHB', row, col, 0, len(value), 1) + value.encode('utf-16le'))
    data = bof(0x10) + rec(0x0200, struct.pack('<IIHHH', 0, len(matrix), 0, len(matrix[0]), 0))
    for r, row in enumerate(matrix):
        for c, value in enumerate(row):
            if value is None:
                continue
            if isinstance(value, (int, float)):
                data += rec(0x0203, struct.pack('<HHHd', r, c, 0, value))
            else:
                data += label(r, c, str(value))
    data += rec(0x000A)
    globals_ = bof(5) + rec(0x0042, struct.pack('<H', 1200)) + rec(0x0022, struct.pack('<H', 0))
    globals_ += rec(0x00E0, struct.pack('<HHH', 0, 0, 0xfff0) + bytes(14))
    def bound(offset):
        return rec(0x0085, struct.pack('<IBBBB', offset, 0, 0, 4, 1) + 'Data'.encode('utf-16le'))
    start = len(globals_) + len(bound(0)) + 4
    payload = globals_ + bound(start) + rec(0x000A) + data
    size = max(4096, ((len(payload) + 511) // 512) * 512)
    assert size // 512 < 125
    header = bytearray(512)
    header[:8] = bytes.fromhex('D0CF11E0A1B11AE1')
    struct.pack_into('<HHHHH', header, 24, 0x3e, 3, 0xfffe, 9, 6)
    struct.pack_into('<IIIIIIIII', header, 40, 0, 1, 1, 0, 4096, 0xfffffffe, 0, 0xfffffffe, 0)
    struct.pack_into('<109I', header, 76, 0, *([0xffffffff] * 108))
    count = size // 512
    fat = [0xfffffffd, 0xfffffffe] + list(range(3, 2 + count)) + [0xfffffffe]
    fat += [0xffffffff] * (128 - len(fat))
    def entry(name, kind, first, length, child=0xffffffff):
        result = bytearray(128)
        encoded = (name + '\0').encode('utf-16le')
        result[:len(encoded)] = encoded
        struct.pack_into('<HBBIII', result, 64, len(encoded), kind, 1, 0xffffffff, 0xffffffff, child)
        struct.pack_into('<IQ', result, 116, first, length)
        return result
    directory = entry('Root Entry', 5, 0xfffffffe, 0, 1) + entry('Workbook', 2, 2, size) + bytes(256)
    path.write_bytes(bytes(header) + struct.pack('<128I', *fat) + directory + payload.ljust(size, b'\0'))


def main():
    target = Path(sys.argv[1]).resolve()
    assert target.parent == Path(__file__).resolve().parents[1] / '.tools'
    assert target.name.startswith('p34-') and not target.exists()
    target.mkdir()
    cases = []

    def case(name, rows, *, total=None, no_total=True, codes=None, classes=None, excluded=None,
             headers=None, mapping=None, reject=None, native_only=False, binary=False, raw_code_token=None):
        path = target / (name + ('.xls' if binary else '.xlsx'))
        head = headers or HEADERS
        if binary:
            xls(path, [head] + rows)
        else:
            workbook = openpyxl.Workbook()
            sheet = workbook.active
            sheet.title = 'Data'
            sheet.append(head)
            for row in rows:
                sheet.append(row)
            # Other sheets never silently replace the current first-sheet contract.
            workbook.create_sheet('Ignored').append(['Do not parse this sheet'])
            workbook.save(path)
            workbook.close()
        if raw_code_token is not None:
            output = BytesIO()
            with ZipFile(path) as archive, ZipFile(output, 'w', ZIP_DEFLATED) as rewritten:
                for item in archive.infolist():
                    data = archive.read(item.filename)
                    if item.filename == 'xl/worksheets/sheet1.xml':
                        xml = ET.fromstring(data)
                        ns = '{http://schemas.openxmlformats.org/spreadsheetml/2006/main}'
                        cell = xml.find(f'.//{ns}c[@r="A2"]')
                        for child in list(cell):
                            cell.remove(child)
                        cell.set('t', 'n')
                        ET.SubElement(cell, ns + 'v').text = raw_code_token
                        data = ET.tostring(xml)
                    rewritten.writestr(item, data)
            path.write_bytes(output.getvalue())
        request = dict(mapping=mapping or MAPPING, code_overrides=codes, classification_overrides=classes,
                       excluded_excel_rows=excluded, total_excel_row=total, no_total_row=no_total)
        result = None
        if not native_only:
            try:
                result = legacy.validate_trial_balance_contents(path.read_bytes(), path.name, request['mapping'],
                    codes, classes, excluded, total, no_total)
                assert reject is None, (name, 'Expected rejection')
            except ValueError:
                assert reject is not None, name
        cases.append(dict(name=name, file=path.name, sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                          request=request, expected=result, reject=reject, native_only=native_only))
        return result

    # These expectations are independently hand-derived, not copied from C#.
    balanced = [['100', 'Assets', 999, 0, 0, 0, 999, 0],
                ['100-01', 'Cash', 100, 0, 25, 5, 120, 0],
                ['200', 'Equity', 0, 999, 0, 0, 0, 999],
                ['200-01', 'Capital', 0, 100, 5, 25, 0, 120],
                ['', 'ERP Total', 100, 100, 30, 30, 120, 120]]
    expected = case('balanced-control', balanced, total=6, no_total=False)
    assert expected['valid'] and list(expected['source_leaf_totals'].values()) == ['100', '100', '30', '30', '120', '120']
    case('balanced-binary', balanced, total=6, no_total=False, binary=True)
    difference = [['A', 'Cash', 100, 0, 0, 0, 90, 0]]
    for token, code in (('1.0', '1'), ('1E3', '1000')):
        numeric = case('numeric-code-' + code, difference, raw_code_token=token)
        assert numeric['account_classifications'][0][0] == code
    expected = case('advisory-differences', difference)
    assert expected['valid'] and expected['source_leaf_totals']['closing_dr_column'] == '90'
    assert expected['draft_reconciliation']['adjusted_totals']['closing_dr_column'] == '100'
    assert expected['draft_reconciliation']['adjusted_totals']['closing_cr_column'] == '100'
    case('difference-control', difference + [['', 'Total', 100, 0, 0, 0, 90, 0]], total=3, no_total=False)
    case('control-mismatch', difference + [['', 'Total', 100, 0, 0, 0, 91, 0]], total=3, no_total=False)
    case('tolerance-at', [['A', 'Cash', 100, 0, 0, 0, '99.99', 0]])
    case('tolerance-over', [['A', 'Cash', 100, 0, 0, 0, '99.9899', 0]])
    case('commas-parentheses', [['001', 'اردو café', '(1,234.50)', '', '0.25', 0, '-1234.25', '']])
    case('binary-text', [['001', 'Cash', '(1,234.50)', '', '0.25', 0, '-1234.25', '']], binary=True)
    case('duplicates', [['001', 'One', 0, 0, 0, 0, 0, 0], ['001', 'Two', 0, 0, 0, 0, 0, 0]])
    subtotal = [['A', 'Cash', 10, 0, 0, 0, 10, 0], ['', 'Subtotal', 999, 0, 0, 0, 999, 0]]
    case('blank-code', subtotal)
    case('excluded-subtotal', subtotal, excluded=[3])
    case('subtotal-as-account', subtotal, codes={'3': 'B'})
    case('date-code', [[datetime(2026, 4, 1), 'Date code', 0, 0, 0, 0, 0, 0]])
    case('date-corrected', [[datetime(2026, 4, 1), 'Date code', 0, 0, 0, 0, 0, 0]], codes={'2': '000-01'})
    case('parent-date-warning', [['2026-04-01', 'Parent', 0, 0, 0, 0, 0, 0], ['2026-04-01-1', 'Leaf', 0, 0, 0, 0, 0, 0]])
    case('unlinked-parent', [['BAD', 'Parent', 0, 0, 0, 0, 0, 0], ['10-01', 'Leaf', 10, 0, 0, 0, 10, 0]], classes={'2': 'parent'})
    case('padding-hierarchy', [['1', 'Root', 0, 0, 0, 0, 0, 0], ['01-002', 'Leaf', 0, 0, 0, 0, 0, 0]])
    case('ambiguous-padding', [['1', 'Root', 0, 0, 0, 0, 0, 0], ['01', 'Other', 0, 0, 0, 0, 0, 0], ['001-02', 'Leaf', 0, 0, 0, 0, 0, 0]])
    case('raw-prefix-wins', [['1', 'Root', 0, 0, 0, 0, 0, 0], ['01', 'Other', 0, 0, 0, 0, 0, 0], ['1-02', 'Leaf', 0, 0, 0, 0, 0, 0]])
    case('mapped-only', [['A', '', 0, 0, 0, 0, 0, 0, 'keep raw'], [None] * 8 + ['not an account']], headers=HEADERS + ['Unmapped'])
    for value in ('NaN', 'Infinity', '#REF!', 'not a number'):
        case('invalid-' + str(len(cases)), [['A', 'Bad amount', value, 0, 0, 0, 0, 0]])
    case('no-accounts', [])
    case('only-parents', [['A', 'Parent', 0, 0, 0, 0, 0, 0]], classes={'2': 'parent'})
    case('choice-missing', difference, no_total=False, reject='total_choice_required')
    case('choice-both', difference, total=2, reject='total_choice_required')
    case('total-outside', difference, total=8, no_total=False, reject='invalid_total_row')
    case('total-corrected', difference, total=2, no_total=False, codes={'2': 'B'}, reject='invalid_total_row')
    case('exclusion-code', difference, excluded=[2], reject='invalid_exclusion')
    case('exclusion-duplicate', subtotal, excluded=[3, 3], reject='invalid_exclusion')
    case('override-outside', difference, codes={'1': 'B'}, reject='invalid_correction')
    case('classification-invalid', difference, classes={'2': 'other'}, reject='invalid_correction')
    case('missing-mapping', difference, mapping={key: value for key, value in MAPPING.items() if key != 'closing_cr_column'}, reject='invalid_mapping')
    case('duplicate-mapping', difference, mapping={**MAPPING, 'closing_cr_column': 'Closing Dr'}, reject='invalid_mapping')
    case('duplicate-headers', difference, headers=HEADERS[:7] + ['Closing Dr'], native_only=True, reject='ambiguous_mapping')
    case('precision', [['001', 'Exact source', '9007199254740993', 0, 0, 0, '9007199254740993', 0]], native_only=True)
    case('numeric-code-precision', difference, raw_code_token='9007199254740991.1', native_only=True)
    case('aggregate-precision', [['A', 'Large', '10000000000000000', 0, 0, 0, '10000000000000000', 0],
                                 ['B', 'Small', 1, 0, 0, 0, 1, 0]], native_only=True)
    case('rollforward-precision', [['A', 'Small movement', '1e100', 0, 1, 0, '1e100', 0]], native_only=True)
    case('out-of-range', [['001', 'Too large', '1' * 1001, 0, 0, 0, 0, 0]], native_only=True)
    case('missing-formula', [['001', 'Missing cached total', '=1+2', 0, 0, 0, 0, 0]], native_only=True)
    large = target / 'large.xlsx'
    workbook = openpyxl.Workbook(write_only=True)
    sheet = workbook.create_sheet('Data')
    sheet.append(HEADERS)
    for i in range(10000):
        amounts = [1.25, 0, 0, 0, 1.25, 0] if i % 2 == 0 else [0, 1.25, 0, 0, 0, 1.25]
        sheet.append([f'{i:06}', 'Synthetic'] + amounts)
    workbook.save(large)
    (target / 'oracle.json').write_text(json.dumps(cases, ensure_ascii=False, allow_nan=False, default=str), encoding='utf-8')
    print(f'P34 oracle: {len(cases)} source/legacy cases and 10,000-row bounded fixture.')


if __name__ == '__main__':
    # A pure validation oracle must never read or write application records.
    def forbidden(*args, **kwargs):
        raise AssertionError('P34 oracle attempted database access')
    with patch.multiple(legacy, execute_query_pg=forbidden, execute_write_pg=forbidden,
                        execute_batch_pg=forbidden, get_raw_conn_pg=forbidden, log_audit=forbidden):
        main()
