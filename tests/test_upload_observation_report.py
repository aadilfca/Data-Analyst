"""Generated recovery evidence: full vouchers, original values and safe retries."""

from io import BytesIO
from datetime import datetime

from openpyxl import load_workbook

from services.upload_observation_report import build_observation_report


COLUMNS = [{'column_name': name, 'data_type': kind} for name, kind in [
    ('Voucher ID', 'Text'), ('Account code', 'Text'), ('Description', 'Text'),
    ('Debit', 'Numeric'), ('Credit', 'Numeric')]]
SCOPE = {'client_id': 5, 'template_id': 1, 'start': '2026-04-01', 'end': '2026-04-30'}


def evidence(count=2, removed=False):
    previous = [{'id': i + 1, 'extract_date': '2026-03-31', 'row_data': {
        'Voucher ID': 'V1', 'Account code': '0002', 'Description': '=SOURCE()' if i == 0 else 'Old line',
        'Debit': 123.456789 if i == 0 else 0, 'Credit': 0 if i == 0 else 123.456789}}
        for i in range(count)]
    observation = {'voucher_id': 'V1', 'document_group_id': '1-V1',
        'status': 'Removed' if removed else 'Changed', 'changed_fields': ['Description', 'Transaction Date'],
        'old_ids': [r['id'] for r in previous], 'incoming_rows': [] if removed else [
            {**r, 'extract_date': '2026-04-15', 'row_data': {**r['row_data'], 'Description': 'Revised line'}}
            for r in previous]}
    return [observation], previous


def test_report_retains_all_lines_identifiers_precision_literal_text_and_dates():
    observations, previous = evidence()
    data = build_observation_report(observations, previous, COLUMNS, SCOPE,
        {'client': 'Example client', 'template': 'Transaction Report'})
    workbook = load_workbook(BytesIO(data))
    assert workbook.sheetnames == ['Observations', 'Previous vouchers', 'Incoming vouchers']
    summary = workbook['Observations']
    assert 'Example client' in summary['A2'].value
    assert [summary.cell(6, i).value for i in (1, 2, 4, 5, 8)] == ['V1', 'Changed', 2, 2, 'Yes']
    old, new = workbook['Previous vouchers'], workbook['Incoming vouchers']
    assert old.max_row == new.max_row == 7
    assert old['E6'].value == '0002' and old['E6'].data_type == 's'
    assert old['F6'].value == '=SOURCE()' and old['F6'].data_type == 's'
    assert old['G6'].value == 123.456789 and old['G7'].value == 0
    assert isinstance(old['C6'].value, datetime) and old['C6'].number_format == 'dd/mm/yyyy'
    assert new['C6'].value == datetime(2026, 4, 15)
    for sheet in workbook:
        assert not sheet.sheet_view.showGridLines
        assert sheet.auto_filter.ref and sheet.freeze_panes == ('C6' if sheet.title == 'Observations' else 'E6')
        assert not any(cell.data_type == 'f' for row in sheet for cell in row)


def test_report_retries_are_byte_identical_and_removed_vouchers_have_no_incoming_lines():
    observations, previous = evidence(removed=True)
    first = build_observation_report(observations, previous, COLUMNS, SCOPE)
    second = build_observation_report(observations, previous, COLUMNS, SCOPE)
    assert first == second
    workbook = load_workbook(BytesIO(first))
    assert workbook['Observations']['E6'].value == 0
    assert workbook['Incoming vouchers'].max_row == 5


def test_685_line_voucher_report_never_truncates_either_source_version():
    observations, previous = evidence(685)
    workbook = load_workbook(BytesIO(build_observation_report(observations, previous, COLUMNS, SCOPE)))
    assert workbook['Previous vouchers'].max_row == workbook['Incoming vouchers'].max_row == 690
    assert workbook['Observations']['D6'].value == workbook['Observations']['E6'].value == 685


def test_partial_upload_report_labels_incoming_rows_as_an_extract():
    observations, previous = evidence()
    workbook = load_workbook(BytesIO(build_observation_report(observations, previous, COLUMNS, SCOPE,
        {'incoming_title': 'Incoming extract'})))
    assert workbook.sheetnames[-1] == 'Incoming extract'
