"""Deterministic Excel evidence for uploaded changes and complete vouchers.

Uses the application's existing OpenPyXL export stack; no extra runtime is
required on the server. The workbook is part of the verified recovery ZIP.
"""

from datetime import date, datetime
from io import BytesIO
import json
import re
import zipfile

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter


GREEN, INK = 'E2EFDA', '24364B'


def _raw(record):
    value = record.get('row_data') or {}
    return json.loads(value) if isinstance(value, str) else value


def _text(value):
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, default=str)
    return re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', '', str(value))


def _cell(sheet, row, column, value, *, text=False):
    cell = sheet.cell(row, column)
    if value is not None:
        cell.value = _text(value) if text or isinstance(value, (dict, list)) else value
        if text or isinstance(cell.value, str):
            # ERP descriptions may begin with '='; evidence must remain text.
            cell.data_type = 's'
            cell.number_format = '@'
    cell.font = Font(name='Calibri', size=11, color=INK)
    cell.alignment = Alignment(horizontal='left' if text else 'right', vertical='top', wrap_text=True)
    return cell


def _heading(sheet, title, context, headers, widths, source=None):
    sheet.sheet_view.showGridLines = False
    for index, width in enumerate(widths, 1):
        sheet.column_dimensions[get_column_letter(index)].width = width
    provenance = 'Incoming file: ' + source if source else 'Source: previous stored vouchers and the incoming upload.'
    for row, label in enumerate([title, context, provenance], 1):
        sheet.merge_cells(start_row=row, start_column=1, end_row=row, end_column=len(headers))
        cell = _cell(sheet, row, 1, label, text=True)
        cell.font = Font(name='Calibri', size=16 if row == 1 else 11, bold=row == 1, color=INK)
        if row != 1:
            cell.fill = PatternFill('solid', fgColor='F1F5F9')
        sheet.row_dimensions[row].height = 32 if row == 1 else 30
    for index, label in enumerate(headers, 1):
        cell = _cell(sheet, 5, index, label, text=True)
        cell.alignment = Alignment(horizontal='center', vertical='center', wrap_text=True)
        cell.fill = PatternFill('solid', fgColor=GREEN)
        cell.font = Font(name='Calibri', size=11, bold=True, color=INK)
        cell.border = Border(bottom=Side(style='thin', color='9CB48F'))
    sheet.row_dimensions[5].height = 34
    sheet.freeze_panes = 'C6'
    sheet.print_title_rows = '1:5'
    sheet.sheet_properties.pageSetUpPr.fitToPage = True
    sheet.page_setup.orientation = 'landscape'
    sheet.page_setup.paperSize = sheet.PAPERSIZE_A3
    sheet.page_setup.fitToWidth, sheet.page_setup.fitToHeight = 1, 0


def _dates(records):
    return ', '.join(sorted({str(r['extract_date'])[:10] for r in records if r.get('extract_date')}))


def build_observation_report(observations, old_rows, template_columns, scope, labels=None):
    """Return XLSX bytes without inventing old/new line pairings or losing rows."""
    labels = labels or {}
    previous = {record['id']: record for record in old_rows}
    context = f"{labels.get('client', 'Client ' + str(scope['client_id']))} / {labels.get('template', 'Template ' + str(scope['template_id']))}"
    if scope.get('start'):
        context += f" / Declared period {scope['start']} to {scope['end']}"
    if scope.get('account_code'):
        context += ' / Account ' + str(scope['account_code'])
    workbook = Workbook()
    workbook.remove(workbook.active)
    workbook.properties.creator = 'Internal Audit App'
    workbook.properties.created = workbook.properties.modified = datetime(2000, 1, 1)
    summary = workbook.create_sheet('Observations')
    _heading(summary, 'Upload observation report', context,
             ['Voucher ID', 'Observation', 'Fields to review', 'Previous lines', 'Incoming lines',
              'Previous dates', 'Incoming dates', 'Date moved outside declared period'],
             [24, 19, 42, 16, 16, 25, 25, 28], labels.get('source'))
    detail_sheets = []
    for name in ('Previous vouchers', labels.get('incoming_title', 'Incoming vouchers')):
        sheet = workbook.create_sheet(name)
        _heading(sheet, name, context,
                 ['Observation', 'Line sequence', 'Stored / validated date'] + [c['column_name'] for c in template_columns],
                 [18, 15, 23] + [22 if c.get('data_type') != 'Numeric' else 18 for c in template_columns], labels.get('source'))
        sheet.freeze_panes = 'E6'  # Keep the voucher ID visible beside its lines.
        for index, column in enumerate(template_columns, 4):
            if any(word in column['column_name'].casefold() for word in ('detail', 'description', 'name')):
                sheet.column_dimensions[get_column_letter(index)].width = 44
        detail_sheets.append(sheet)
    for row_index, observation in enumerate(observations, 6):
        before = [previous[key] for key in observation['old_ids']]
        after = observation.get('incoming_rows', [])
        moved = bool(after and scope.get('start') and any(
            not record.get('extract_date') or not scope['start'] <= str(record['extract_date'])[:10] <= scope['end']
            for record in before))
        values = [observation['voucher_id'], observation['status'], ', '.join(observation['changed_fields']),
                  len(before), len(after), _dates(before), _dates(after), 'Yes' if moved else 'No']
        for index, value in enumerate(values, 1):
            cell = _cell(summary, row_index, index, value, text=index not in (4, 5))
            if index in (4, 5):
                cell.number_format = '#,##0'
                cell.alignment = Alignment(horizontal='right', vertical='top')
        summary.row_dimensions[row_index].height = 46
        for sheet, records in zip(detail_sheets, (before, after)):
            for sequence, record in enumerate(records, 1):
                position = max(sheet.max_row + 1, 6)
                _cell(sheet, position, 1, observation['status'], text=True)
                _cell(sheet, position, 2, sequence).number_format = '#,##0'
                day = date.fromisoformat(str(record['extract_date'])[:10]) if record.get('extract_date') else None
                _cell(sheet, position, 3, day).number_format = 'dd/mm/yyyy'
                sheet.cell(position, 3).alignment = Alignment(horizontal='center', vertical='top')
                data = _raw(record)
                for index, column in enumerate(template_columns, 4):
                    value = data.get(column['column_name'])
                    numeric = column.get('data_type') == 'Numeric' and isinstance(value, (int, float)) and not isinstance(value, bool)
                    cell = _cell(sheet, position, index, value, text=not numeric)
                    if numeric:
                        cell.number_format = '#,##0.00########;(#,##0.00########);0'
                        cell.alignment = Alignment(horizontal='right', vertical='top')
                longest = max((len(str(value or '')) for value in data.values()), default=0)
                sheet.row_dimensions[position].height = min(180, max(32, 16 * (longest // 40 + 1)))
    for sheet in workbook:
        sheet.auto_filter.ref = f'A5:{get_column_letter(sheet.max_column)}{max(5, sheet.max_row)}'
        sheet.print_options.horizontalCentered = True
        sheet.print_area = sheet.dimensions
    output = BytesIO()
    workbook.save(output)
    # ZIP member times and OpenPyXL's modified timestamp must not change retry
    # checksums for the same approved source versions.
    deterministic = BytesIO()
    with zipfile.ZipFile(output) as source, zipfile.ZipFile(deterministic, 'w', zipfile.ZIP_DEFLATED) as target:
        for name in sorted(source.namelist()):
            content = source.read(name)
            if name == 'docProps/core.xml':
                # Preserve namespace declarations used by xsi:type QNames.
                content = re.sub(rb'(<dcterms:(?:created|modified)\b[^>]*>)[^<]*(</dcterms:(?:created|modified)>)',
                                 rb'\g<1>2000-01-01T00:00:00Z\g<2>', content)
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            target.writestr(info, content)
    return deterministic.getvalue()


def build_change_report(change_detection, template_columns, scope, labels=None, imported_at=None):
    """Build a human-readable, complete row report for one validated import plan.

    The workbook is deliberately labelled as pending until the database transaction
    succeeds. Previous and incoming lines are separate so no correction pairing is
    inferred by the report itself.
    """
    labels = labels or {}
    columns = [column['column_name'] for column in template_columns]
    incoming = []
    previous = []
    for item in change_detection.get('changed', []):
        incoming.append(('Changed', item.get('new_row_data') or {}))
        previous.append(('Changed', item.get('old_row_data') or {}))
    incoming.extend(('Added', item.get('row_data', item) if isinstance(item, dict) else item)
                    for item in change_detection.get('new', []))
    previous.extend(('Removed', item.get('old_row_data') or {})
                     for item in change_detection.get('missing', []))
    informational = [
        (item.get('client_data_id'), field, (item.get('old_field_values') or {}).get(field), value)
        for item in change_detection.get('informational_updates', [])
        for field, value in (item.get('field_updates') or {}).items()
    ]

    workbook = Workbook()
    workbook.remove(workbook.active)
    workbook.properties.creator = 'Internal Audit App'
    workbook.properties.created = workbook.properties.modified = datetime(2000, 1, 1)
    context = f"{labels.get('client', 'Client ' + str(scope.get('client_id')))} / {labels.get('template', 'Template ' + str(scope.get('template_id')))}"
    summary = workbook.create_sheet('Import summary')
    summary.sheet_view.showGridLines = False
    summary_rows = [
        ('Upload change report', ''),
        ('Import outcome', 'Check the app result popup; this workbook contains the validated changes.'),
        ('Client and template', context),
        ('Source file', labels.get('source', '')),
        ('Prepared at (Asia/Karachi)', imported_at or ''),
        ('Report period', f"{scope.get('start') or ''} – {scope.get('end') or ''}".strip(' –')),
        ('Ledger account', scope.get('account_code') or ''),
        ('New rows', len(change_detection.get('new', []))),
        ('Rows with informational field refreshes', len(change_detection.get('informational_updates', []))),
        ('Revised rows', len(change_detection.get('changed', []))),
        ('Removed rows', len(change_detection.get('missing', []))),
    ]
    for row_index, (label, value) in enumerate(summary_rows, 1):
        _cell(summary, row_index, 1, label, text=True)
        _cell(summary, row_index, 2, value, text=isinstance(value, str))
    summary.column_dimensions['A'].width = 30
    summary.column_dimensions['B'].width = 72
    for row in summary.iter_rows():
        for cell in row:
            cell.font = Font(name='Calibri', size=11, bold=cell.column == 1, color=INK)
            cell.alignment = Alignment(vertical='top', wrap_text=True)
    summary['A1'].font = Font(name='Calibri', size=16, bold=True, color=INK)
    summary.freeze_panes = 'A2'

    def write_rows(name, records):
        sheet = workbook.create_sheet(name)
        headers = ['Change'] + columns
        sheet.sheet_view.showGridLines = False
        sheet.append(headers)
        for cell in sheet[1]:
            cell.font = Font(name='Calibri', size=11, bold=True, color=INK)
            cell.fill = PatternFill('solid', fgColor=GREEN)
            cell.alignment = Alignment(horizontal='center', vertical='center', wrap_text=True)
        sheet.freeze_panes = 'B2'
        sheet.auto_filter.ref = f'A1:{get_column_letter(max(1, len(headers)))}{max(1, len(records) + 1)}'
        sheet.column_dimensions['A'].width = 16
        for index, column in enumerate(template_columns, 2):
            name_text = column['column_name']
            sheet.column_dimensions[get_column_letter(index)].width = (
                42 if any(word in name_text.casefold() for word in ('detail', 'description', 'name'))
                else 18 if column.get('data_type') == 'Numeric' else 22
            )
        for action, data in records:
            row = [action] + [data.get(column) for column in columns]
            sheet.append([None] * len(headers))
            for index, value in enumerate(row, 1):
                numeric = index > 1 and template_columns[index - 2].get('data_type') == 'Numeric'
                cell = _cell(sheet, sheet.max_row, index, value, text=not numeric)
                if numeric and isinstance(value, (int, float)) and not isinstance(value, bool):
                    cell.number_format = '#,##0.00########;(#,##0.00########);0'
            sheet.row_dimensions[sheet.max_row].height = 34
        sheet.sheet_properties.pageSetUpPr.fitToPage = True
        sheet.page_setup.orientation = 'landscape'
        sheet.page_setup.fitToWidth, sheet.page_setup.fitToHeight = 1, 0
        return sheet

    write_rows('Incoming changes', incoming)
    write_rows('Previous records', previous)
    if informational:
        sheet = workbook.create_sheet('Informational refreshes')
        sheet.sheet_view.showGridLines = False
        sheet.append(['Client Data ID', 'Field', 'Previous value', 'Incoming value'])
        for cell in sheet[1]:
            cell.font = Font(name='Calibri', size=11, bold=True, color=INK)
            cell.fill = PatternFill('solid', fgColor=GREEN)
            cell.alignment = Alignment(horizontal='center', vertical='center', wrap_text=True)
        sheet.freeze_panes = 'A2'
        sheet.auto_filter.ref = f'A1:D{len(informational) + 1}'
        for column, width in zip('ABCD', (18, 24, 34, 34)):
            sheet.column_dimensions[column].width = width
        for client_data_id, field, old_value, new_value in informational:
            sheet.append([client_data_id, field, old_value, new_value])
    output = BytesIO()
    workbook.save(output)
    deterministic = BytesIO()
    with zipfile.ZipFile(output) as source, zipfile.ZipFile(deterministic, 'w', zipfile.ZIP_DEFLATED) as target:
        for name in sorted(source.namelist()):
            content = source.read(name)
            if name == 'docProps/core.xml':
                content = re.sub(rb'(<dcterms:(?:created|modified)\b[^>]*>)[^<]*(</dcterms:(?:created|modified)>)',
                                 rb'\g<1>2000-01-01T00:00:00Z\g<2>', content)
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            target.writestr(info, content)
    return deterministic.getvalue()
