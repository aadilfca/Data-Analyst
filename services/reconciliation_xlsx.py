"""Professional Excel exports for ledger reconciliation reports.

The summary engine owns voucher selection and accounting calculations.  This
module only turns those canonical pandas outputs into a reviewable workbook.
It deliberately accepts a completed summary rather than loading application
data, which keeps export formatting separate from report-source selection.
"""

from __future__ import annotations

from io import BytesIO
from pathlib import Path
import re
import json
import tempfile
from copy import copy
from typing import Any, Mapping

import pandas as pd
from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side, NamedStyle
from openpyxl.cell import WriteOnlyCell
from openpyxl.worksheet._writer import WorksheetWriter, ALL_TEMP_FILES
from openpyxl.worksheet.worksheet import Worksheet
from openpyxl.utils import get_column_letter


NAVY = "1F4E78"
LIGHT_BLUE = "D9EAF7"
PALE_GREEN = "E2F0D9"
PALE_ORANGE = "FCE4D6"
PALE_RED = "F4CCCC"
TEXT = "1F2937"
MUTED = "5B6573"
WHITE = "FFFFFF"
GRID = "D9E2F3"
EXCEL_BLUE = "0070C0"
EXCEL_BROWN = "7E350E"

MONEY_FORMAT = '#,##0;[Red](#,##0);-'
DATE_FORMAT = "dd-mmm-yy"
LEDGERWISE_MONEY_FORMAT = '#,##0.00;[Red](#,##0.00);0.00'

_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


class _ReconciliationWorkbook(Workbook):
    """Streaming workbook with job-owned scratch files and deterministic cleanup.

    Openpyxl has no public per-workbook scratch-directory option. Its writer
    adapter is confined here; shared process-wide tempfile settings never change.
    """
    def __init__(self, directory=None, progress=None):
        super().__init__(write_only=True)
        self.scratch = tempfile.TemporaryDirectory(prefix='reconciliation-', dir=directory)
        self.progress = progress or (lambda *args: None)
        self.styles = {}
        for name, bold, color, fill in [
            ('body', False, TEXT, None), ('total', True, TEXT, LIGHT_BLUE),
            ('header', True, WHITE, NAVY), ('title', True, NAVY, None),
            ('note', False, MUTED, None), ('matched', True, TEXT, PALE_GREEN),
            ('failed', True, TEXT, PALE_RED),
        ]:
            for money in (False, True, 'integer'):
                style = NamedStyle(name=f'recon_{name}_{money}')
                style.font = Font(name='Arial', size=14 if name == 'title' else 9 if name == 'note' else 10,
                                  bold=bold, color=color)
                style.alignment = Alignment(horizontal='right' if money else 'left', vertical='center', wrap_text=True)
                if fill:
                    style.fill = PatternFill('solid', fgColor=fill)
                style.number_format = '#,##0.00;[Red](#,##0.00);-' if money is True else '0' if money == 'integer' else 'General'
                self.add_named_style(style)
                self.styles[name, money] = style._style

    def save(self, filename):
        try:
            super().save(filename)
        finally:
            self.close()

    def close(self):
        for sheet in self.worksheets:
            writer = sheet._writer
            if writer is not None:
                try:
                    if not sheet.closed:
                        sheet.close()
                finally:
                    if Path(writer.out).exists():
                        writer.cleanup()
        self.scratch.cleanup()


def _reconciliation_append(ws, values, *, style='body', money_columns=(), status_column=None):
    """Assign prebuilt styles once; imported strings remain literal Excel text."""
    cells = []
    for column, value in enumerate(values, 1):
        if isinstance(value, (dict, list, tuple)):
            value = json.dumps(value, ensure_ascii=False, default=str)
        if isinstance(value, str) and len(value) > 32767:
            raise ValueError(f'{ws.title}: source text exceeds Excel cell capacity; export was stopped without truncating evidence.')
        cell = WriteOnlyCell(ws, value=_safe_cell(value))
        if isinstance(cell.value, str):
            cell.data_type = 's'
        chosen = style
        if column == status_column and isinstance(value, (int, float)):
            chosen = 'matched' if value == 0 else 'failed'
        number_style = column in money_columns
        if not number_style and isinstance(value, int) and not isinstance(value, bool):
            number_style = 'integer'
        # Never mutate an assigned shared StyleArray after it has been serialized.
        cell._style = ws.parent.styles[chosen, number_style]
        if ws.title.startswith('Working Paper') and column == 3 and value and len(str(value)) == 24:
            cell.hyperlink = "#'Adjustment Support'!A1"
        cells.append(cell)
    ws.append(cells)


def _reconciliation_sheet(workbook, title, scope, headers):
    ws = workbook.create_sheet(title)
    ws.sheet_view.showGridLines = False
    ws.freeze_panes = 'B5'
    ws.sheet_properties.pageSetUpPr.fitToPage = True
    ws.page_setup.orientation = 'landscape'
    ws.page_setup.paperSize = Worksheet.PAPERSIZE_A4
    ws.page_setup.fitToWidth, ws.page_setup.fitToHeight = 1, 0
    ws.print_title_rows = '1:4'
    ws.oddFooter.center.text = 'Page &P of &N'
    for index, header in enumerate(headers, 1):
        ws.column_dimensions[get_column_letter(index)].width = min(48, max(16, len(header)+4))
    if title.startswith('Working Paper'):
        for column, width in [('A', 76), ('B', 24), ('C', 29)]:
            ws.column_dimensions[column].width = width
    ws.merged_cells.add(f'A1:{get_column_letter(len(headers))}1')
    ws.merged_cells.add(f'A2:{get_column_letter(len(headers))}2')
    ws.row_dimensions[1].height = 24
    ws.row_dimensions[2].height = 30
    # Direct only this writer's XML files to the private export directory.
    path = str(Path(workbook.scratch.name) / f'sheet-{len(workbook.worksheets)}.xml')
    ALL_TEMP_FILES.append(path)
    ws._writer = WorksheetWriter(ws, out=path)
    ws._writer.write_top()
    display_title = ('Ledger / Cashflow reconciliation — ' + title.removeprefix('Working Paper ')
                     if title.startswith('Working Paper ') else title)
    _reconciliation_append(ws, [display_title], style='title')
    _reconciliation_append(ws, [scope], style='note')
    ws.append([])
    _reconciliation_append(ws, headers, style='header')
    return ws


def build_cashflow_reconciliation_workbook(result, *, source, support, account_types, comparisons,
                                         temporary_directory=None, progress=None):
    workbook = _ReconciliationWorkbook(temporary_directory, progress)
    try:
        return _populate_cashflow_reconciliation_workbook(workbook, result, source=source, support=support,
            account_types=account_types, comparisons=comparisons)
    except BaseException:
        workbook.close()
        raise


def _populate_cashflow_reconciliation_workbook(workbook, result, *, source, support, account_types, comparisons):
    """Format the approved working paper without recalculating accounting amounts.

    Original occurrences appear once in each source sheet. Adjustment and posting
    schedules reference them by account/voucher and retain repeated source rows.
    """
    start, end = result['period_start'], result['period_end']
    scope = (f"Client {result['client_id']} | {start} to {end} | "
             f"{result['treasury_view'].title()} | {result['receipt_presentation'].title()} receipts")
    selection = result.get('account_selection')
    if selection:
        scope += f" | {len(selection['account_codes'])} selected Cash/Bank accounts; see Report Controls"
    money = '#,##0.00;[Red](#,##0.00);-'
    for view, part in result['views'].items():
        workbook.progress('Writing working paper ' + view.title(), 0, None)
        ws = _reconciliation_sheet(workbook, 'Working Paper ' + view.title(), scope,
                                   ['Particulars', 'Amount', 'Support reference'])
        row = 4

        def line(label, amount=None, reference='', total=False):
            nonlocal row
            row += 1
            ws.row_dimensions[row].height = 15 * (len(label) // 72 + 1)
            _reconciliation_append(ws, [label, 'Unavailable' if amount is None else amount, reference],
                style='total' if total else 'body', money_columns=(2,),
                status_column=2 if 'difference' in label.lower() else None)
            return row

        for side, title in [('receipts', 'Receipts'), ('payments', 'Payments')]:
            row += 1
            _reconciliation_append(ws, [title, '', ''], style='header')
            values = part['overall'][side]
            line(title + ' as per ledgers', values['ledger'], total=True)
            for adjustment in part['adjustments']:
                amount = adjustment['total'][side]
                if amount == 0:
                    continue
                label = ('' if amount is None else 'Less: ' if amount < 0 else 'Add: ') + adjustment['label']
                if adjustment.get('category'):
                    label += ' — ' + adjustment['category']
                line(label, amount, adjustment['key'])
            line('Total presentation adjustments', values['adjustments'], total=True)
            line('Adjusted ' + side + ' as per ledgers', values['adjusted_ledger'], total=True)
            line(title + ' as per Cashflow report', values['cashflow'], total=True)
            line('Unexplained difference (adjusted ledger − Cashflow)', values['difference'], total=True)
        row += 2
        ws.append([])
        _reconciliation_append(ws, ['Opening and closing proof', '', ''], style='header')
        proof = part['overall_balance_proof']
        for label, value in [
            ('Opening balance as per ledgers', proof['ledger_opening']),
            ('Add: Receipts as per Cashflow report', part['overall']['receipts']['cashflow']),
            ('Less: Payments as per Cashflow report', part['overall']['payments']['cashflow']),
            ('Separate internal-transfer movement (signed)', proof['separate_transfer_movement']),
            ('Separate mixed-voucher movement (signed)', proof['separate_mixed_movement']),
            ('Calculated closing balance', proof['calculated_closing']),
            ('Closing balance as per ledgers', proof['ledger_closing']),
            ('Closing difference (ledger − calculated)', proof['difference']),
        ]:
            line(label, value, total=True)
        row += 2
        ws.append([])
        _reconciliation_append(ws, ['Review controls', 'Status', ''], style='header')
        for label, value in [
            ('Receipt/payment amounts', part['amount_status']),
            ('Whole-range posting comparison', part['posting_comparison']['posting_status']),
            ('Treasury allocation', part['allocation_status']),
            ('Transaction Report extract completeness', result['transaction_coverage_status']),
        ]:
            row += 1
            _reconciliation_append(ws, [label, value, 'See Report Controls'])
        ws.print_area = f'A1:C{row}'

    adjustment_rows = (dict(entry, adjustment_key=key) for key, entries in support.items() for entry in entries)
    _write_reconciliation_records(workbook, 'Adjustment Support', scope,
        [('adjustment_key', 'Support reference'), ('treasury_view', 'Proof'), ('voucher_id', 'Voucher'),
         ('period_key', 'Period'), ('reason', 'Reason'), ('label', 'Adjustment'), ('category', 'Category'),
         ('receipts', 'Receipts adjustment'), ('payments', 'Payments adjustment')], adjustment_rows)
    _write_reconciliation_records(workbook, 'Posting Comparison', scope,
        [('proof_view', 'Proof'), ('account_code', 'Account code'), ('treasury_type', 'Treasury'),
         ('voucher_id', 'Voucher'), ('unkeyed_identity', 'Unkeyed reference'), ('status', 'Posting status'),
         ('difference_fields', 'Fields differing'), ('ledger_line_count', 'Ledger occurrences'),
         ('transaction_line_count', 'Voucher occurrences'), ('matched_occurrences', 'Matched occurrences'),
         ('ledger_movement', 'Ledger movement'), ('transaction_movement', 'Voucher movement'),
         ('movement_difference', 'Movement difference'), ('note', 'Explanation')],
        (dict(row, proof_view=view) for view, comparison in comparisons.items() for row in comparison['findings']),
        total=sum(len(value['findings']) for value in comparisons.values()))

    selected = {code for code, kind in account_types.items()
                if result['treasury_view'] in ('combined', 'split') or kind.casefold() == result['treasury_view']}
    if selection:
        selected &= set(selection['account_codes'])
    ledger_rows = [row for row in source['ledger_rows'] if row.get('account_code') in selected]
    references = {str(row.get('voucher_id') or '').strip() for row in ledger_rows}
    references.update(str(row.get('voucher_id') or '').strip() for row in source['transaction_rows']
                      if row.get('account_code') in selected)
    references.update(str(entry['voucher_id']).strip() for entries in support.values() for entry in entries)
    references.discard('')
    voucher_rows = [row for row in source['transaction_rows']
                    if row.get('account_code') in selected or str(row.get('voucher_id') or '').strip() in references]
    columns = [('id', 'Source row ID'), ('date', 'Date'), ('account_code', 'Account code'),
        ('account_name', 'Account name'), ('voucher_id', 'Voucher'), ('debit', 'Debit (original)'),
        ('credit', 'Credit (original)'), ('description', 'Description'), ('in_period', 'Within report period'),
        ('upload_id', 'Upload ID'), ('template_id', 'Template ID'), ('version', 'Version'),
        ('uploaded_at', 'Imported at'), ('source_fingerprint', 'Source fingerprint')]
    if selection:
        columns.append(('account_scope', 'Account scope'))

    def evidence(rows):
        for row in rows:
            value = dict(row)
            value['date'] = row.get('date', row.get('extract_date'))
            day = str(value['date'])[:10]
            value['in_period'] = 'Yes' if start <= day <= end else 'Context outside period / invalid date'
            value['source_fingerprint'] = row.get('source_fingerprint', row.get('fingerprint'))
            if selection:
                value['account_scope'] = ('Selected Cash/Bank account' if row.get('account_code') in selected else
                    'Other Cash/Bank account — voucher context' if row.get('account_code') in selection['configured_account_codes']
                    else 'Counter-account — voucher context')
            # Exact source text survives Excel's fifteen-digit numeric precision.
            for key in ('debit', 'credit'):
                if value.get(key) is not None:
                    value[key] = str(value[key])
            yield value
    _write_reconciliation_records(workbook, 'Ledger Evidence', scope, columns, evidence(ledger_rows), total=len(ledger_rows))
    _write_reconciliation_records(workbook, 'Voucher Evidence', scope, columns, evidence(voucher_rows), total=len(voucher_rows))

    controls = [(key.replace('_', ' ').title(), result.get(key)) for key in (
        'client_id', 'template_id', 'period_start', 'period_end', 'group_by', 'treasury_view',
        'receipt_presentation', 'evidence_fingerprint', 'cache_identity', 'configuration_key', 'report_revision',
        'transaction_coverage_status', 'complete', 'scope_note', 'review_controls', 'diagnostics')]
    controls += [('Evidence amounts', 'Original debit/credit values are text to preserve source precision; working-paper amounts are numeric to two decimals.'),
        ('Evidence context', 'Outside-period rows support voucher and opening-balance review; they are not additional report-period movements.'),
        ('Import timestamps', 'Import metadata does not certify ERP extraction times.'),
        ('Ledger source rows', len(ledger_rows)), ('Voucher source rows', len(voucher_rows)),
        ('Adjustment operations', sum(len(entries) for entries in support.values())),
        ('Posting groups including matched', sum(len(c['findings']) for c in comparisons.values()))]
    if selection:
        controls.extend([('Account selection', selection),
            ('Account context', 'Only selected Cash/Bank accounts contribute ledger amounts. Other voucher accounts support classification and are not additional selected-account receipts/payments.')])
    for view, part in result['views'].items():
        controls.extend((view.title() + ' ' + key.replace('_', ' '), part.get(key)) for key in (
            'amount_status', 'status', 'allocation_status', 'unresolved'))
        controls.extend((view.title() + ' ' + key.replace('_', ' '), comparisons[view].get(key)) for key in (
            'posting_status', 'posting_comparison_complete', 'counts', 'account_totals', 'coverage_diagnostics'))
        balance_controls = part['balance_controls']
        controls.extend((view.title() + ' balance ' + key, value) for key, value in balance_controls.items() if key != 'periods')
        for period in balance_controls.get('periods', []):
            label = view.title() + ' balance ' + period['key']
            controls.append((label, {key: value for key, value in period.items() if key != 'accounts'}))
            controls.extend((label + ' account ' + str(account.get('account_code')), account)
                            for account in period.get('accounts', []))
    controls.extend(('Ledger upload ' + str(upload.get('id')), upload) for upload in source['uploads']
                    if upload.get('ledger_account_code') in selected)
    def control_records(label, value):
        # Give each coverage/account field its own inspectable row rather than a JSON block.
        if isinstance(value, dict) and value:
            for key, child in value.items():
                yield from control_records(label + ' / ' + str(key).replace('_', ' '), child)
        elif isinstance(value, (list, tuple)) and value:
            for index, child in enumerate(value, 1):
                yield from control_records(label + ' / ' + str(index), child)
        elif isinstance(value, str) and len(value) > 1000:
            # Upload metadata can arrive as serialized JSON or long source notes.
            # Numbered, readable parts preserve every character without one oversized cell.
            count = (len(value) + 999) // 1000
            for index in range(count):
                yield dict(control=f'{label} / Part {index + 1} of {count}',
                           value=value[index * 1000:(index + 1) * 1000])
        else:
            yield dict(control=label, value='Unavailable' if value is None else
                       'None recorded' if isinstance(value, (dict, list, tuple)) else value)
    _write_reconciliation_records(workbook, 'Report Controls', scope,
        [('control', 'Control'), ('value', 'Value')],
        (row for label, value in controls for row in control_records(label, value)))
    return workbook


def _write_reconciliation_records(workbook, title, scope, columns, records, *, total=None):
    """Stream each complete record once; no retained evidence cells or finishing scans."""
    sheet = None
    row_number = 4
    page = 0
    written = 0
    money_columns = tuple(index for index, (key, _) in enumerate(columns, 1)
                          if key in {'receipts', 'payments', 'ledger_movement', 'transaction_movement', 'movement_difference'})
    for record in records:
        if sheet is None or row_number >= 1048576:
            if sheet is not None:
                sheet.auto_filter.ref = f'A4:{get_column_letter(len(columns))}{row_number}'
                sheet.print_area = f'A1:{get_column_letter(len(columns))}{row_number}'
            page += 1
            name = title if page == 1 else title[:26] + f' {page}'
            sheet = _reconciliation_sheet(workbook, name, scope, [label for _, label in columns])
            row_number = 4
        if written % 250 == 0:
            workbook.progress('Writing ' + title, written, total)
        row_number += 1
        values = [record.get(key) for key, _ in columns]
        # Bound row height without revisiting any already-written cell.
        length = max((len(str(value or '')) for value in values), default=0)
        sheet.row_dimensions[row_number].height = min(409, 15 * (length // 44 + 1))
        _reconciliation_append(sheet, values, money_columns=money_columns)
        # The row dimension has already been serialized with this row.
        sheet.row_dimensions.pop(row_number, None)
        written += 1
    if sheet is None:
        sheet = _reconciliation_sheet(workbook, title, scope, [label for _, label in columns])
    sheet.auto_filter.ref = f'A4:{get_column_letter(len(columns))}{row_number}'
    sheet.print_area = f'A1:{get_column_letter(len(columns))}{row_number}'
    workbook.progress('Writing ' + title, written, total)


def export_cashflow_reconciliation_xlsx(result, *, destination=None, **kwargs):
    """Write a job file directly, or return bytes for existing integrations/tests."""
    workbook = build_cashflow_reconciliation_workbook(result, **kwargs)
    output = BytesIO() if destination is None else destination
    try:
        workbook.progress('Finalizing Excel file', 0, None)
        workbook.save(output)
    finally:
        workbook.close()
    return output.getvalue() if destination is None else None


def build_reconciliation_workbook(
    summary: Mapping[str, Any],
    *,
    report_label: str | None = None,
    cross_group_check: pd.DataFrame | None = None,
    opening_balances: Mapping[str, Any] | None = None,
) -> Workbook:
    """Build a Bank or Cash reconciliation workbook from engine output.

    ``summary`` must be the dictionary returned by
    :func:`services.ledger_group_summary.build_group_summary`.  All numeric
    values are copied as typed spreadsheet numbers.  The workbook contains no
    formulas that could display stale cached values before Excel recalculates.
    """

    label = str(report_label or summary.get("group_label") or "Ledger").strip() or "Ledger"
    months = [str(month) for month in summary.get("months", [])]
    if not months:
        raise ValueError("summary must contain at least one reporting month")

    workbook = Workbook()
    default = workbook.active
    workbook.remove(default)
    workbook.calculation.calcMode = "auto"
    workbook.calculation.fullCalcOnLoad = True
    workbook.calculation.forceFullCalc = True

    summary_sheet = workbook.create_sheet(f"{label} Monthly Summary")
    _write_monthly_summary(summary_sheet, summary, label, months)

    transfer_name = "Inter-Cashbook Transfers" if label.casefold() == "cash" else "Transfer Schedule"
    transfer_sheet = workbook.create_sheet(transfer_name)
    _write_transfer_schedule(transfer_sheet, summary, label, months)

    if label.casefold() == "cash" and _frame_has_rows(summary.get("related_group_breakdown")):
        related_sheet = workbook.create_sheet("Inter company")
        _write_related_schedule(related_sheet, summary, label)

    review_sheet = workbook.create_sheet("Review Schedule")
    _write_review_schedule(review_sheet, summary, label)

    if cross_group_check is not None:
        cross_sheet = workbook.create_sheet("Cross-check vs Bank Summary")
        _write_cross_group_check(cross_sheet, cross_group_check, label)

    proof_sheet = workbook.create_sheet("Reconciliation Proof")
    _write_combined_proof(proof_sheet, summary, label, opening_balances)

    account_proof_sheet = workbook.create_sheet(
        "Reconciliation Proof - By Book" if label.casefold() == "cash" else "Reconciliation Proof - By Bank"
    )
    _write_account_proof(account_proof_sheet, summary, label)

    for sheet in workbook.worksheets:
        _finish_sheet(sheet)
    return workbook


def export_reconciliation_xlsx(
    summary: Mapping[str, Any],
    *,
    report_label: str | None = None,
    cross_group_check: pd.DataFrame | None = None,
    opening_balances: Mapping[str, Any] | None = None,
) -> bytes:
    """Return a generated reconciliation workbook as XLSX bytes."""

    workbook = build_reconciliation_workbook(
        summary,
        report_label=report_label,
        cross_group_check=cross_group_check,
        opening_balances=opening_balances,
    )
    output = BytesIO()
    workbook.save(output)
    return output.getvalue()


def build_cashflow_workbook(
    preview: Mapping[str, Any],
    *,
    period_start: str | None = None,
    period_end: str | None = None,
    group_by: str | None = None,
) -> Workbook:
    """Build an XLSX workbook from the serialized Cashflows report preview."""
    periods = [item for item in (preview.get("periods") or []) if isinstance(item, Mapping)]
    period_keys = [str(item.get("key") or "") for item in periods]
    period_labels = [str(item.get("label") or item.get("key") or "") for item in periods]
    if not period_keys or any(not key for key in period_keys):
        raise ValueError("Cashflows export requires reporting periods")

    workbook = Workbook()
    workbook.remove(workbook.active)
    workbook.calculation.calcMode = "auto"
    workbook.calculation.fullCalcOnLoad = True
    workbook.calculation.forceFullCalc = True

    summary_sheet = workbook.create_sheet("Cashflows Summary")
    treasury_view = str(preview.get("treasury_view") or "combined")
    summary_preview = dict(preview)
    if treasury_view == "split":
        _write_cashflow_split_summary_sheet(
            summary_sheet,
            summary_preview,
            period_keys,
            period_labels,
            period_start=period_start,
            period_end=period_end,
            group_by=group_by,
        )
    else:
        summary_preview["category_totals"] = preview.get("treasury_view_totals") or preview.get("category_totals") or []
        _write_cashflow_summary_sheet(
            summary_sheet,
            summary_preview,
            period_keys,
            period_labels,
            period_start=period_start,
            period_end=period_end,
            group_by=group_by,
        )
    ledgerwise_sheet = workbook.create_sheet("Ledgerwise Summary")
    _write_cashflow_ledgerwise_summary_sheet(
        ledgerwise_sheet,
        summary_preview,
        period_keys,
        period_labels,
        period_start=period_start,
        period_end=period_end,
        group_by=group_by,
    )
    selected_scope = bool(preview.get('account_selection'))
    _write_cashflow_detail_sheet(
        workbook.create_sheet("Internal Transfers"),
        "Internal Transfers",
        [_cashflow_transfer_export_record(record) for record in (preview.get("transfer_details") or [])],
        [
            ("date", "Date", False),
            ("voucher_id", "Voucher", False),
            ("transferor", "Transferor", False),
            ("transferee", "Transferee", False),
            ("amount", "Complete-voucher amount (context)" if selected_scope else "Amount", True),
            ("variance", "Variance", True),
            ("status", "Status", False),
            ("voucher_detail", "Description", False),
            ("bank_movement", "Voucher Bank movement (context)" if selected_scope else "Signed Bank movement", True),
            ("cash_movement", "Voucher Cash movement (context)" if selected_scope else "Signed Cash movement", True),
        ] + ([('selection_inflow', 'Selected-account transfer receipts', True),
              ('selection_outflow', 'Selected-account transfer payments', True),
              ('selection_net_movement', 'Selected-account transfer net', True),
              ('evidence_scope', 'Evidence scope', False)] if selected_scope else []),
    )
    _write_cashflow_detail_sheet(
        workbook.create_sheet("Customer Deductions"),
        "Customer Deductions",
        preview.get("deduction_details") or [],
        [("period", "Period", False), ("category", "Category", False), ("account_name", "Account", False), ("amount", "Amount", True)],
    )
    _write_cashflow_detail_sheet(
        workbook.create_sheet("Review Required"),
        "Review Required",
        (preview.get("categories_needing_review") or []) + (preview.get('selection_review_details') or []),
        [("voucher_id", "Voucher", False), ("period", "Period", False), ("account_code", "Account code", False), ("account_name", "Account", False), ("fallback_category", "Review category", False)]
        + ([('period_key', 'Selection review period', False), ('reason', 'Selection review reason', False),
            ('receipts', 'Selected receipts under review', True), ('payments', 'Selected payments under review', True)] if selected_scope else []),
    )

    _write_cashflow_detail_sheet(
        workbook.create_sheet("Cashflows Ledger Drill-down"),
        "Cashflows Ledger Drill-down",
        preview.get("ledger_rollups") or [],
        [
            ("category", "Category", False),
            ("period", "Period", False),
            ("account_name", "Ledger account", False),
            ("account_code", "Account code", False),
            ("treasury_type", "Cash / Bank", False),
            ("amount", "Amount", True),
            ("line_count", "Lines", True),
        ],
        note="Canonical ledger roll-ups matching the selected Cashflows report and active export filters.",
    )

    # Detail sheets use the same signed party totals as the browser.
    customer_columns = [
        ("period_label", "Period", False), ("customer_account_name", "Customer", False),
        ("customer_account_code", "Account code", False),
        ("customer_receipts", "Receipts", True), ("customer_reversals", "Reversals / refunds", True),
        ("customer_gross_amount", "Net receipts", True), ("bank_charges", "Bank charges", True),
        ("withholding_tax", "WHT", True), ("other_deductions", "Other deductions", True),
        ("cashflow_amount", "Cashflow amount", True), ("status", "Status", False),
    ]
    vendor_columns = [
        ("period_label", "Period", False), ("account_name", "Vendor", False),
        ("account_code", "Account code", False), ("category", "Cashflow category", False),
        ("payments", "Payments", True), ("reversals", "Reversals / refunds", True),
        ("net_payments", "Net payments", True),
    ]
    for key, title, columns in (
        ("customer_payment_account_period_totals", "Customer Period Totals", customer_columns),
        ("customer_payment_customer_totals", "Customer Totals", customer_columns),
        ("customer_payment_details", "Customer Transactions", [("date", "Date", False), ("voucher_id", "Voucher", False), *customer_columns]),
        ("vendor_payment_period_totals", "Vendor Period Totals", vendor_columns),
        ("vendor_payment_details", "Vendor Transactions", [("date", "Date", False), ("voucher_id", "Voucher", False), *vendor_columns]),
    ):
        if preview.get(key):
            _write_cashflow_detail_sheet(workbook.create_sheet(title), title, preview[key], columns)

    controls = workbook.create_sheet("Export Controls")
    _write_title(controls, "Cashflows Export Controls", "Control totals and selection used for this workbook.", 2)
    drilldown_split = _cashflow_drilldown_export_split(preview.get("ledger_rollups") or [])
    export_filters = preview.get("export_filters") or {}
    control_rows = [
        ("Period start", period_start or ""),
        ("Period end", period_end or ""),
        ("Grouping", group_by or preview.get("group_by") or "month"),
        ("Treasury view", preview.get("treasury_view") or "combined"),
        ("Treasury reconciliation", (preview.get("treasury_view_reconciliation") or {}).get("status") or "Not available"),
        ("Treasury maximum variance", (preview.get("treasury_view_reconciliation") or {}).get("max_abs_variance") or 0),
        ("Treasury mixed / unresolved", (preview.get("treasury_view_reconciliation") or {}).get("mixed_amount") or 0),
        ("Receipt presentation", preview.get("receipt_presentation") or "net"),
        ("Summary category filter", export_filters.get("summary_filter") or "All categories"),
        ("Hide zero rows", "Yes" if export_filters.get("hide_zero_rows") else "No"),
        ("Drill-down search", export_filters.get("search") or "All"),
        ("Drill-down category", export_filters.get("category") or "All"),
        ("Drill-down Cash / Bank", export_filters.get("treasury_type") or "All"),
        ("Drill-down period", export_filters.get("period_key") or "All"),
        ("Selected vouchers", int(preview.get("selected_voucher_count") or 0)),
        ("Internal transfers", int(preview.get("internal_transfer_count") or 0)),
        ("Customer deductions", int(preview.get("deduction_count") or 0)),
        ("Review required", int(preview.get("category_review_count") or 0)),
        ("Drill-down rows", drilldown_split["row_count"]),
        ("Drill-down detail total", drilldown_split["detail_total"]),
        ("Drill-down Bank amount", drilldown_split["bank_amount"]),
        ("Drill-down Cash amount", drilldown_split["cash_amount"]),
        ("Drill-down mixed / unresolved amount", drilldown_split["mixed_or_unresolved_amount"]),
        ("Source Cash / Bank status", ((preview.get("drilldown_controls") or {}).get("cash_bank_split") or {}).get("status") or "Not loaded"),
    ]
    if selected_scope:
        selection = preview['account_selection']
        control_rows += [('Account selection', 'Selected Cash/Bank accounts'),
            ('Account selection version', selection['version']),
            ('Selection allocation reviews', preview.get('selection_review_count', 0)),
            ('Transfer context', 'Complete voucher amounts are supporting context; selected transfer receipts/payments are shown separately.')]
        control_rows += [('Selected Cash/Bank account ' + str(index), code)
                         for index, code in enumerate(selection['account_codes'], 1)]
    row = 4
    _write_table(controls, row, ["Control", "Value"], header=True)
    for control, value in control_rows:
        row += 1
        _write_table(controls, row, [control, value])

    if preview.get('treasury_movement_proof'):
        _write_cashflow_movement_proof(workbook.create_sheet('Treasury Movement Proof'), preview)
    if selected_scope:
        for sheet in workbook.worksheets:
            note = sheet.cell(2, 1)
            note.value = str(note.value or '') + f" | {len(selection['account_codes'])} selected Cash/Bank accounts; see Export Controls."
    for sheet in workbook.worksheets:
        _finish_sheet(sheet)
    for row_cells in ledgerwise_sheet.iter_rows(min_row=5, max_col=5):
        for cell in row_cells[2:5]:
            if cell.value is not None:
                cell.number_format = LEDGERWISE_MONEY_FORMAT
    if 'Treasury Movement Proof' in workbook.sheetnames:
        proof = workbook['Treasury Movement Proof']
        for row in range(5, proof.max_row + 1):
            for column in [*range(3, 9), *range(11, 17)]:
                proof.cell(row, column).number_format = '#,##0.00;[Red](#,##0.00);-'
    return workbook


def _write_cashflow_movement_proof(ws, preview: Mapping[str, Any]) -> None:
    """Export full-scope source/ledger controls independently of category filters."""
    headers = ['Period', 'Treasury', 'Category net', 'Pure internal transfers',
               'Mixed voucher Treasury movement', 'Explained movement', 'Source movement', 'Source difference',
               'Movement proof', 'Category allocation', 'Unallocated category net',
               'Ledger opening', 'Calculated opening', 'Ledger closing', 'Calculated closing', 'Ledger difference', 'Ledger status']
    _write_title(ws, 'Treasury Movement Proof',
        'Full report scope, independent of category filters. Signed debit minus credit. Mixed voucher movements use actual Treasury legs; category allocation remains under review. Combined categories already include mixed vouchers. Unavailable ledger evidence is left blank.', len(headers))
    _write_table(ws, 4, headers, header=True)
    balances = preview.get('balance_reconciliation') or {}
    for index, record in enumerate(preview.get('treasury_movement_proof') or [], 5):
        view = record['treasury_type']
        selected_view = str(preview.get('treasury_view') or 'combined')
        evidence = balances if (view == selected_view or view == 'combined' and selected_view in ('combined', 'split')) else (balances.get('treasury_breakdown') or {}).get(view, {})
        period = next((row for row in evidence.get('periods', []) if row['key'] == record['period_key']), {})
        values = [record['period_key'], view, *[record.get(key) for key in (
            'category_movement', 'internal_transfer_movement', 'mixed_voucher_movement',
            'explained_movement', 'source_movement', 'variance')], record.get('status'), record.get('allocation_status'),
            record.get('unallocated_category_movement'), *[period.get(key) for key in (
                'opening_total', 'calculated_opening', 'closing_total', 'calculated_closing', 'variance', 'status')]]
        _write_table(ws, index, values)


def export_cashflow_xlsx(
    preview: Mapping[str, Any],
    *,
    period_start: str | None = None,
    period_end: str | None = None,
    group_by: str | None = None,
) -> bytes:
    """Return a Cashflows workbook matching the visible report payload."""
    workbook = build_cashflow_workbook(
        preview,
        period_start=period_start,
        period_end=period_end,
        group_by=group_by,
    )
    output = BytesIO()
    workbook.save(output)
    return output.getvalue()


def _cashflow_section_records(
    preview: Mapping[str, Any],
    period_keys: list[str],
    section: str,
) -> list[tuple[str, list[float], float]]:
    records: list[tuple[str, list[float], float]] = []
    for record in preview.get("category_totals") or []:
        values = [_number(record.get(key)) or 0.0 for key in period_keys]
        signed_total = _number(record.get("Total"))
        if signed_total is None:
            signed_total = sum(values)
        if section == "Receipts" and signed_total < 0:
            continue
        if section == "Payments" and signed_total >= 0:
            continue
        records.append((str(record.get("category") or "Unlabelled"), values, abs(signed_total)))
    return records


def _cashflow_display_value(value: float, section: str) -> float:
    """Orient a value for section presentation while retaining numeric cells.

    Receipts keep the canonical sign so payment reversals render in brackets.
    Payments invert the canonical sign so normal payments are positive and
    receipt reversals render in brackets through the workbook number format.
    """
    return -value if section == "Payments" else value


def _local_amount_label(value: float | None) -> str:
    amount = _number(value) or 0.0
    if amount == 0:
        return "-"
    absolute = abs(amount)
    if absolute >= 10000000:
        label = f"{absolute / 10000000:.0f} crore"
    elif absolute >= 100000:
        label = f"{absolute / 100000:.0f} lakh"
    elif absolute >= 1000:
        label = f"{absolute / 1000:.0f} thousand"
    else:
        label = f"{absolute:.0f} Rs"
    return f"({label})" if amount < 0 else label



def _cashflow_split_records(preview: Mapping[str, Any], period_keys: list[str]) -> list[tuple[str, list[dict[str, float]], dict[str, float]]]:
    grouped: dict[str, dict[str, dict[str, float]]] = {}
    for record in preview.get("treasury_view_totals") or []:
        category = str(record.get("category") or "Unlabelled")
        period_key = str(record.get("period_key") or "")
        if period_key not in period_keys:
            continue
        grouped.setdefault(category, {})[period_key] = {
            "bank": _number(record.get("bank_amount")) or 0.0,
            "cash": _number(record.get("cash_amount")) or 0.0,
            "mixed": _number(record.get("mixed_amount")) or 0.0,
            "total": _number(record.get("total_amount")) or 0.0,
        }
    output: list[tuple[str, list[dict[str, float]], dict[str, float]]] = []
    for category, period_values in grouped.items():
        values = [period_values.get(key, {"bank": 0.0, "cash": 0.0, "mixed": 0.0, "total": 0.0}) for key in period_keys]
        totals = {
            "bank": sum(value["bank"] for value in values),
            "cash": sum(value["cash"] for value in values),
            "mixed": sum(value["mixed"] for value in values),
            "total_raw": sum(value["total"] for value in values),
        }
        totals["total"] = abs(totals["total_raw"])
        output.append((category, values, totals))
    return output


def _write_cashflow_split_header(ws, row: int, period_labels: list[str]) -> int:
    sub_headers = ("Bank", "Cash", "Mixed / unresolved", "Total")
    width = 1 + ((len(period_labels) + 1) * len(sub_headers)) + 2
    ws.cell(row, 1, "Category")
    ws.merge_cells(start_row=row, start_column=1, end_row=row + 1, end_column=1)
    column = 2
    for label in [*period_labels, "Selected total"]:
        ws.cell(row, column, _safe_cell(label))
        ws.merge_cells(start_row=row, start_column=column, end_row=row, end_column=column + 3)
        for offset, sub_header in enumerate(sub_headers):
            ws.cell(row + 1, column + offset, sub_header)
        column += 4
    for label in ("% of section", "Local total"):
        ws.cell(row, column, label)
        ws.merge_cells(start_row=row, start_column=column, end_row=row + 1, end_column=column)
        column += 1
    for header_row in (row, row + 1):
        for header_column in range(1, width + 1):
            cell = ws.cell(header_row, header_column)
            cell.fill = PatternFill("solid", fgColor=NAVY if header_row == row else LIGHT_BLUE)
            cell.font = Font(name="Arial", size=10, bold=True, color=WHITE if header_row == row else NAVY)
            cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
            cell.border = Border(bottom=Side(style="thin", color=WHITE if header_row == row else NAVY))
    return width


def _write_cashflow_split_summary_sheet(
    ws,
    preview: Mapping[str, Any],
    period_keys: list[str],
    period_labels: list[str],
    *,
    period_start: str | None,
    period_end: str | None,
    group_by: str | None,
) -> None:
    view_label = "Cash and Bank individually"
    width = 1 + ((len(period_keys) + 1) * 4) + 2
    reconciliation = preview.get("treasury_view_reconciliation") or {}
    _write_title(
        ws,
        f"Cashflows Summary - {view_label} ({period_labels[0]} to {period_labels[-1]})",
        f"Visible Cashflows report for {period_start or 'selected start'} to {period_end or 'selected end'}; grouping: {group_by or preview.get('group_by') or 'month'}. Bank + Cash + Mixed / unresolved = Total. Reconciliation: {reconciliation.get('status') or 'Not available'}.",
        width,
    )
    row = 4
    for section in ("Receipts", "Payments"):
        records = [record for record in _cashflow_split_records(preview, period_keys) if (record[2]["total_raw"] < 0) == (section == "Payments")]
        _style_cashflow_section_band(ws, row, width)
        ws.cell(row, 1, f"{section} — percentages calculated within this section")
        row += 1
        _write_cashflow_split_header(ws, row, period_labels)
        row += 2
        if not records:
            _write_table(ws, row, [f"No {section.casefold()} identified"])
            row += 2
            continue
        basis = sum(record[2]["total"] for record in records)
        for category, values, totals in records:
            row_values: list[Any] = [category]
            for value in values:
                row_values.extend(_cashflow_display_value(value[field], section) for field in ("bank", "cash", "mixed", "total"))
            row_values.extend(_cashflow_display_value(totals[field], section) for field in ("bank", "cash", "mixed"))
            row_values.extend((totals["total"], f"{(totals['total'] / basis * 100) if basis else 0:.2f}%", _local_amount_label(totals["total"])))
            _write_table(ws, row, row_values)
            _style_cashflow_summary_values(ws, row, total_columns=[2 + (index * 4) + 3 for index in range(len(period_keys) + 1)], percentage_column=width - 1, local_total_column=width)
            row += 1
        period_totals = [{field: sum(values[index][field] for _, values, _ in records) for field in ("bank", "cash", "mixed", "total")} for index in range(len(period_keys))]
        total_values: list[Any] = [f"Total {section}"]
        for period_total in period_totals:
            total_values.extend(_cashflow_display_value(period_total[field], section) for field in ("bank", "cash", "mixed", "total"))
        total_values.extend(_cashflow_display_value(sum(record[2][field] for record in records), section) for field in ("bank", "cash", "mixed"))
        total_values.extend((basis, "100.00%", _local_amount_label(basis)))
        _write_table(ws, row, total_values, total=True)
        _style_cashflow_summary_values(ws, row, total_columns=[2 + (index * 4) + 3 for index in range(len(period_keys) + 1)], percentage_column=width - 1, local_total_column=width)
        row += 3
    _write_cashflow_summary_proof_rows(ws, preview, period_keys, row, split=True)
    ws.freeze_panes = "B7"

def _write_cashflow_summary_sheet(
    ws,
    preview: Mapping[str, Any],
    period_keys: list[str],
    period_labels: list[str],
    *,
    period_start: str | None,
    period_end: str | None,
    group_by: str | None,
) -> None:
    _write_title(
        ws,
        f"Cashflows Summary ({period_labels[0]} to {period_labels[-1]})",
        f"Visible Cashflows report for {period_start or 'selected start'} to {period_end or 'selected end'}; grouping: {group_by or preview.get('group_by') or 'month'}. Receipts and payments use separate percentage bases.",
        len(period_keys) + 5,
    )
    row = 4
    headers = ["Category", *period_labels, "Total", "% of section", "Local total"]
    for section in ("Receipts", "Payments"):
        records = _cashflow_section_records(preview, period_keys, section)
        ws.cell(row, 1, f"{section} — percentages calculated within this section")
        _style_cashflow_section_band(ws, row, len(headers))
        row += 1
        _write_table(ws, row, headers, header=True)
        row += 1
        if not records:
            _write_table(ws, row, [f"No {section.casefold()} identified"])
            row += 2
            continue
        basis = sum(total for _, _, total in records)
        for category, values, total in records:
            _write_table(
                ws,
                row,
                [category, *[_cashflow_display_value(value, section) for value in values], total, f"{(total / basis * 100) if basis else 0:.2f}%", _local_amount_label(total)],
            )
            _style_cashflow_summary_values(ws, row, total_columns=[len(period_keys) + 2], percentage_column=len(period_keys) + 3, local_total_column=len(period_keys) + 4)
            row += 1
        period_totals = [sum(values[index] for _, values, _ in records) for index in range(len(period_keys))]
        _write_table(
            ws,
            row,
            [f"Total {section}", *[_cashflow_display_value(value, section) for value in period_totals], basis, "100.00%", _local_amount_label(basis)],
            total=True,
        )
        _style_cashflow_summary_values(ws, row, total_columns=[len(period_keys) + 2], percentage_column=len(period_keys) + 3, local_total_column=len(period_keys) + 4)
        row += 3
    _write_cashflow_summary_proof_rows(ws, preview, period_keys, row, split=False)
    ws.freeze_panes = "B6"



def _write_cashflow_summary_proof_rows(ws, preview, period_keys, start_row, *, split):
    """Append the same signed full-scope movement bridge shown on screen."""
    proof = preview.get('treasury_movement_proof') or []
    if not proof:
        return
    view = str(preview.get('treasury_view') or 'combined')
    fields = ['bank', 'cash', 'mixed', 'combined'] if split else [view]
    balances = preview.get('balance_reconciliation') or {}
    def value(key, field, kind):
        record = next((p for p in proof if p['period_key'] == key and p['treasury_type'] == ('combined' if field == 'mixed' else field)), {})
        if kind.startswith('ledger:'):
            if field == 'mixed':
                return None
            source = balances if field == view or field == 'combined' else (balances.get('treasury_breakdown') or {}).get(field, {})
            period = next((p for p in source.get('periods', []) if p['key'] == key), {})
            return period.get(kind.split(':')[1])
        if field == 'mixed':
            amount = record.get('unallocated_category_movement')
            return amount if kind == 'category_movement' else -amount if kind == 'mixed_voucher_movement' and amount is not None else 0
        return record.get(kind)
    descriptors = [('category_movement', 'Category net movement'), ('internal_transfer_movement', 'Pure internal transfers')]
    if any(p.get('mixed_voucher_count') for p in proof):
        descriptors.append(('mixed_voucher_movement', 'Mixed voucher Treasury bridge — allocation under review'))
    descriptors.extend([('explained_movement', 'Net cash movement'), ('ledger:opening_total', 'Opening balances—as per ledgers'),
        ('ledger:calculated_opening', 'Calculated opening balance'), ('ledger:closing_total', 'Closing balances—as per ledgers'),
        ('ledger:calculated_closing', 'Calculated closing balance')])
    for row, (kind, label) in enumerate(descriptors, start_row):
        values = [label]
        for key in period_keys:
            values.extend(value(key, field, kind) for field in fields)
        def total(field):
            if kind == 'ledger:opening_total' or kind == 'ledger:calculated_opening':
                return value(period_keys[0], field, kind)
            if kind.startswith('ledger:'):
                return value(period_keys[-1], field, kind)
            amounts = [value(key, field, kind) for key in period_keys]
            return sum(amounts) if all(amount is not None for amount in amounts) else None
        values.extend(total(field) for field in fields)
        values.extend([None, _local_amount_label(total('combined' if split else view)) if total('combined' if split else view) is not None else 'Unavailable'])
        _write_table(ws, row, values)
        for column in range(2, len(values)-1):
            ws.cell(row, column).number_format = '#,##0.00;[Red](#,##0.00);-'


def _write_cashflow_ledgerwise_summary_sheet(
    ws,
    preview: Mapping[str, Any],
    period_keys: list[str],
    period_labels: list[str],
    *,
    period_start: str | None,
    period_end: str | None,
    group_by: str | None,
) -> None:
    """Write category totals with their underlying Cash/Bank ledger rows."""
    _write_title(
        ws,
        "Ledgerwise Summary",
        f"Category-wise ledger detail for {period_start or 'selected start'} to {period_end or 'selected end'}; grouping: {group_by or preview.get('group_by') or 'month'}. Category rows reconcile to the selected Cashflows Summary.",
        5,
    )
    headers = [
        "Account Code",
        "Account Name/Ledger",
        "Ledger Total Amount (Cash and Bank)",
        "Total Amount as per Cashflows Summary",
        "Difference (if any)",
    ]
    _write_table(ws, 4, headers, header=True)

    summary_totals = _cashflow_ledgerwise_category_totals(preview, period_keys)
    ledger_groups = _cashflow_ledgerwise_ledger_groups(preview.get("ledger_rollups") or [])
    categories = list(summary_totals)
    categories.extend(category for category in ledger_groups if category not in summary_totals)

    row = 5
    for category in categories:
        summary_total = summary_totals.get(category, 0.0)
        ledger_rows = ledger_groups.get(category, [])
        ledger_total_raw = sum(item["amount"] for item in ledger_rows)
        display_sign = -1.0 if summary_total < 0 else 1.0
        display_summary_total = abs(summary_total) if summary_total < 0 else summary_total
        display_ledger_total = ledger_total_raw * display_sign
        _write_table(
            ws,
            row,
            [None, category, display_ledger_total, display_summary_total, display_ledger_total - display_summary_total],
            total=True,
        )
        _style_cashflow_ledgerwise_row(ws, row, category=True)
        row += 1
        for item in ledger_rows:
            _write_table(
                ws,
                row,
                [item["account_code"], item["account_name"], item["amount"] * display_sign, None, None],
            )
            _style_cashflow_ledgerwise_row(ws, row, ledger=True)
            row += 1

    if row == 5:
        _write_table(ws, row, [None, "No ledger rows identified.", None, None, None])
        _style_cashflow_ledgerwise_row(ws, row, ledger=True)
    ws.freeze_panes = "C5"


def _cashflow_ledgerwise_category_totals(
    preview: Mapping[str, Any],
    period_keys: list[str],
) -> dict[str, float]:
    """Normalize combined, single-treasury, and split summary totals by category."""
    totals: dict[str, float] = {}
    records = preview.get("treasury_view_totals") or preview.get("category_totals") or []
    for record in records:
        if not isinstance(record, Mapping):
            continue
        category = str(record.get("category") or "Unlabelled")
        if record.get("Total") is not None:
            amount = _number(record.get("Total")) or 0.0
        elif record.get("total_amount") is not None:
            amount = _number(record.get("total_amount")) or 0.0
        else:
            amount = sum(_number(record.get(key)) or 0.0 for key in period_keys)
        totals[category] = totals.get(category, 0.0) + amount
    return totals


def _cashflow_ledgerwise_ledger_groups(
    records: list[Mapping[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    """Aggregate period-level roll-ups into one row per category and ledger."""
    grouped: dict[str, dict[tuple[str, str], dict[str, Any]]] = {}
    for record in records:
        if not isinstance(record, Mapping):
            continue
        category = str(record.get("category") or "Unlabelled")
        account_code = str(record.get("account_code") or "").strip()
        account_name = str(record.get("account_name") or "").strip()
        key = (account_code, account_name)
        category_rows = grouped.setdefault(category, {})
        item = category_rows.setdefault(
            key,
            {"account_code": account_code, "account_name": account_name or account_code, "amount": 0.0},
        )
        item["amount"] += _number(record.get("amount")) or 0.0
    return {category: list(rows.values()) for category, rows in grouped.items()}


def _style_cashflow_ledgerwise_row(ws, row: int, *, category: bool = False, ledger: bool = False) -> None:
    for column in range(1, 6):
        cell = ws.cell(row, column)
        cell.alignment = Alignment(
            horizontal="left" if column in (1, 2) else "right",
            vertical="center",
            indent=1 if ledger and column == 2 else 0,
        )
        if column in (3, 4, 5):
            cell.number_format = LEDGERWISE_MONEY_FORMAT
        if category:
            cell.fill = PatternFill("solid", fgColor=LIGHT_BLUE)
            cell.font = Font(name="Arial", size=10, bold=True, color=NAVY)
            cell.border = Border(bottom=Side(style="thin", color=NAVY))
        elif ledger:
            cell.font = Font(name="Arial", size=10, color=TEXT)


def _cashflow_transfer_party_label(record: Mapping[str, Any], prefix: str) -> str:
    code = str(record.get(f"{prefix}_account_code") or "").strip()
    name = str(record.get(f"{prefix}_account_name") or "").strip()
    account_type = str(record.get(f"{prefix}_type") or "").strip()
    account = f"{name} ({code})" if name and code and name != code else (name or code)
    return " · ".join(part for part in (account, account_type) if part) or "—"


def _cashflow_transfer_export_record(record: Mapping[str, Any]) -> dict[str, Any]:
    output = dict(record)
    output["transferor"] = _cashflow_transfer_party_label(record, "transferor")
    output["transferee"] = _cashflow_transfer_party_label(record, "transferee")
    output["status"] = str(record.get("status") or "").strip() or (
        "Confirmed" if record.get("matched") else "Review required"
    )
    return output


def _cashflow_drilldown_export_split(records: list[Mapping[str, Any]]) -> dict[str, float | int]:
    amounts = {"Bank": 0.0, "Cash": 0.0, "mixed_or_unresolved": 0.0}
    for record in records:
        amount = _number(record.get("amount")) or 0.0
        treasury_type = str(record.get("treasury_type") or "").strip().casefold()
        if treasury_type == "bank":
            amounts["Bank"] += amount
        elif treasury_type == "cash":
            amounts["Cash"] += amount
        else:
            amounts["mixed_or_unresolved"] += amount
    return {
        "row_count": len(records),
        "detail_total": sum(amounts.values()),
        "bank_amount": amounts["Bank"],
        "cash_amount": amounts["Cash"],
        "mixed_or_unresolved_amount": amounts["mixed_or_unresolved"],
    }


def _write_cashflow_detail_sheet(
    ws,
    title: str,
    records: list[Mapping[str, Any]],
    columns: list[tuple[str, str, bool]],
    *,
    note: str | None = None,
) -> None:
    _write_title(ws, title, note or "Detail rows included in the selected Cashflows report.", len(columns))
    row = 4
    _write_table(ws, row, [label for _, label, _ in columns], header=True)
    if not records:
        _write_table(ws, row + 1, ["No rows identified."])
        return
    for record in records:
        row += 1
        values = []
        for key, _, numeric in columns:
            value = record.get(key)
            if key == "period":
                value = record.get("period_label") or record.get("month") or record.get("period_key")
            if key == "date":
                value = _date_value(value)
            if key == "matched":
                value = "Yes" if value else "Shown separately"
            values.append(_number(value) if numeric else value)
        _write_table(ws, row, values)
        for column, (key, _, numeric) in enumerate(columns, 1):
            ws.cell(row, column).alignment = Alignment(
                horizontal="right" if numeric else "left",
                vertical="center",
            )
            if key == "status":
                _style_status(ws.cell(row, column), record.get(key))
    ws.freeze_panes = "A5"


def save_reconciliation_xlsx(
    summary: Mapping[str, Any],
    path: str | Path,
    *,
    report_label: str | None = None,
    cross_group_check: pd.DataFrame | None = None,
    opening_balances: Mapping[str, Any] | None = None,
) -> Path:
    """Build and save a reconciliation workbook, returning the exact path."""

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(
        export_reconciliation_xlsx(
            summary,
            report_label=report_label,
            cross_group_check=cross_group_check,
            opening_balances=opening_balances,
        )
    )
    return target


def _write_monthly_summary(ws, summary: Mapping[str, Any], label: str, months: list[str]) -> None:
    title = f"{label} Monthly Analysis ({_month_range_label(months)})"
    note = (
        f"Source-backed report for the configured {label.casefold()} account codes. "
        "Every selected voucher is included; internal transfers are shown separately."
    )
    _write_title(ws, title, note, len(months) + 2)

    pivot = _month_frame(summary.get("category_pivot"), months)
    inflows, payments = [], []
    for category, row in pivot.iterrows():
        values = [_number(row.get(month)) for month in months]
        total = sum(value or 0.0 for value in values)
        (inflows if total >= 0 else payments).append((str(category), values, total))

    row = 4
    row = _write_category_section(ws, row, f"SECTION 1: {label.upper()} INFLOWS (net)", inflows, months, payment=False)
    row += 2
    row = _write_category_section(ws, row, f"SECTION 2: {label.upper()} PAYMENTS (net)", payments, months, payment=True)
    row += 2

    net_values = [_number(pivot[month].sum()) for month in months]
    _write_table(
        ws,
        row,
        [f"Net Movement (Inflows - Payments)", *net_values, sum(value or 0.0 for value in net_values)],
        header=False,
        total=True,
    )
    row += 3
    ws.cell(row, 1, "Control: category total versus selected-account movement")
    _style_section_band(ws, row, len(months) + 2)
    row += 1
    checks = summary.get("monthly_balance_checks")
    check_headers = ["Month", "Category Total", "Group Movement", "Variance", "Status"]
    _write_table(ws, row, check_headers, header=True)
    row += 1
    check_rows = []
    if isinstance(checks, pd.DataFrame):
        for _, check in checks.iterrows():
            variance = _number(check.get("variance"))
            check_rows.append(
                [
                    _safe_cell(check.get("month")),
                    _number(check.get("category_total")),
                    _number(check.get("group_total")),
                    variance,
                    "Matched" if bool(check.get("balanced")) else "Variance",
                ]
            )
    if not check_rows:
        check_rows = [["Not available", "Not provided", "Not provided", "Not provided", "Not tested"]]
    for values in check_rows:
        _write_table(ws, row, values)
        _style_status(ws.cell(row, 5), values[-1])
        row += 1
    ws.freeze_panes = "B6"


def _write_category_section(
    ws,
    row: int,
    heading: str,
    records: list[tuple[str, list[float | None], float]],
    months: list[str],
    *,
    payment: bool,
) -> int:
    ws.cell(row, 1, heading)
    _style_section_band(ws, row, len(months) + 2)
    row += 1
    _write_table(ws, row, ["Category", *[_month_label(month) for month in months], "Total"], header=True)
    row += 1
    if not records:
        _write_table(ws, row, ["No categories identified", *([None] * (len(months) + 1))])
        return row + 1
    for category, values, total in records:
        display_values = [(-value if value is not None else None) if payment else value for value in values]
        display_total = -total if payment else total
        _write_table(ws, row, [category, *display_values, display_total])
        row += 1
    totals = [
        sum(((-value if value is not None else 0.0) if payment else (value or 0.0)) for _, values, _ in records for value in [values[index]])
        for index in range(len(months))
    ]
    _write_table(ws, row, [f"Total {heading.split(':', 1)[1].strip()}", *totals, sum(totals)], total=True)
    return row + 1


def _write_transfer_schedule(ws, summary: Mapping[str, Any], label: str, months: list[str]) -> None:
    title = "Cash Transferred Between the 3 Cashbooks" if label.casefold() == "cash" else "Internal Transfer Schedule"
    note = (
        "Internal cashbook movements are excluded from external category totals and shown here for transparency."
        if label.casefold() == "cash"
        else "Transfers between selected accounts are excluded from external category totals and shown here for transparency."
    )
    _write_title(ws, title, note, len(months) + 2)
    pivot = _month_frame(summary.get("internal_transfer_pivot"), months)
    labels = _account_labels(summary)
    row = 4
    if not pivot.empty:
        _write_table(ws, row, ["Account", *[_month_label(month) for month in months], "Total"], header=True)
        row += 1
        for account_code, values in pivot.iterrows():
            numbers = [_number(values.get(month)) for month in months]
            _write_table(ws, row, [labels.get(str(account_code), str(account_code)), *numbers, sum(value or 0 for value in numbers)])
            row += 1
        row += 1
    else:
        _write_table(ws, row, ["No internal transfers identified"])
        row += 2

    ws.cell(row, 1, "Full transaction detail")
    _style_section_band(ws, row, 8)
    row += 1
    headers = ["Voucher ID", "Date", "Account Code", "Account Name", "Voucher Detail", "Debit", "Credit", "Signed Movement"]
    _write_table(ws, row, headers, header=True)
    row += 1
    frame = summary.get("internal_transfers")
    if not _frame_has_rows(frame):
        _write_table(ws, row, ["No internal transfer detail available"])
        return
    for record in frame.to_dict("records"):
        _write_table(
            ws,
            row,
            [
                _safe_cell(record.get("voucher_id")),
                _date_value(record.get("date")),
                _safe_cell(record.get("account_code")),
                _safe_cell(record.get("account_name")),
                _safe_cell(record.get("voucher_detail")),
                _number(record.get("debit")),
                _number(record.get("credit")),
                _number(record.get("signed_movement")),
            ],
        )
        row += 1
    ws.freeze_panes = "A6"


def _write_related_schedule(ws, summary: Mapping[str, Any], label: str) -> None:
    _write_title(
        ws,
        "Inter company",
        "Related-group transactions are listed separately so external category totals remain readable.",
        5,
    )
    _write_table(ws, 3, ["Date", "Voucher", "Type", "Detail", "Amount"], header=True)
    frame = summary.get("related_group_breakdown")
    if not _frame_has_rows(frame):
        _write_table(ws, 4, ["No related-group transactions identified"])
        return
    row = 4
    for record in frame.to_dict("records"):
        amount = _number(record.get("signed_movement"))
        _write_table(
            ws,
            row,
            [
                _date_value(record.get("date")),
                _safe_cell(record.get("voucher_id")),
                "Cash received" if (amount or 0) >= 0 else "Cash paid",
                _safe_cell(record.get("voucher_detail")),
                amount,
            ],
        )
        row += 1
    ws.freeze_panes = "A4"


def _write_review_schedule(ws, summary: Mapping[str, Any], label: str) -> None:
    _write_title(
        ws,
        "Category Review Schedule",
        "These contra lines were not mapped to an active Ledger Summary category. Review before relying on the category totals.",
        10,
    )
    headers = ["Month", "Date", "Voucher ID", "Account Code", "Account Name", "Voucher Detail", "Debit", "Credit", "Suggested Category", "Action"]
    _write_table(ws, 4, headers, header=True)
    frame = summary.get("categories_needing_review")
    if not _frame_has_rows(frame):
        _write_table(ws, 5, ["No unmapped contra lines identified"])
        return
    row = 5
    for record in frame.to_dict("records"):
        _write_table(
            ws,
            row,
            [
                _month_label(record.get("month")),
                _date_value(record.get("date")),
                _safe_cell(record.get("voucher_id")),
                _safe_cell(record.get("account_code")),
                _safe_cell(record.get("account_name")),
                _safe_cell(record.get("voucher_detail")),
                _number(record.get("debit")),
                _number(record.get("credit")),
                _safe_cell(record.get("fallback_category")),
                "Assign category",
            ],
        )
        row += 1
    ws.freeze_panes = "A5"


def _write_cross_group_check(ws, frame: pd.DataFrame, label: str) -> None:
    _write_title(
        ws,
        "Cross-check: Cash Received/Paid to Bank vs. Bank Summary's Cash Balance Line",
        "The related group movement should agree with the opposite group's corresponding line after the configured sign conversion.",
        8,
    )
    _write_table(ws, 4, ["Month", "Cash Summary", "Bank Summary Equivalent", "Variance", "Status"], header=True)
    row = 5
    if not _frame_has_rows(frame):
        _write_table(ws, row, ["Not tested"])
        return
    for record in frame.to_dict("records"):
        status = "Matched" if bool(record.get("status") == "Matched") else "Variance"
        _write_table(
            ws,
            row,
            [
                _safe_cell(record.get("month")),
                _number(record.get("left_value")),
                _number(record.get("right_equivalent")),
                _number(record.get("variance")),
                status,
            ],
        )
        _style_status(ws.cell(row, 5), status)
        row += 1
    ws.freeze_panes = "A5"


def _write_combined_proof(ws, summary: Mapping[str, Any], label: str, opening_balances: Mapping[str, Any] | None) -> None:
    _write_title(
        ws,
        f"Reconciliation Proof: Combined {label} Balance, Month by Month",
        "Opening balance plus this report's monthly net movement is compared with the actual combined closing balance.",
        6,
    )
    headers = ["Month", "Net Movement (this summary)", "Derived Closing Balance", "Actual Ledger Closing Balance", "Variance", "Status"]
    _write_table(ws, 4, headers, header=True)
    row = 5
    if not bool(summary.get("reconciliation_available")):
        _write_table(ws, row, ["Balance inputs not provided. Reconciliation proof is unavailable."])
        ws.cell(row, 1).fill = PatternFill("solid", fgColor=PALE_ORANGE)
        return
    if opening_balances:
        opening = sum(float(value or 0) for value in opening_balances.values())
        _write_table(ws, row, [f"Opening {_month_range_label(summary.get('months', []), opening=True)}", None, opening, "Not provided", "Not provided", "Not tested"])
        ws.cell(row, 4).fill = PatternFill("solid", fgColor=PALE_ORANGE)
        row += 1
    proof = summary.get("reconciliation_proof")
    if not _frame_has_rows(proof):
        _write_table(ws, row, ["Reconciliation proof data is unavailable"])
        return
    for record in proof.to_dict("records"):
        status = _safe_cell(record.get("status")) or "Not tested"
        _write_table(
            ws,
            row,
            [
                _month_label(record.get("month")),
                _number(record.get("net_movement")),
                _number(record.get("derived_closing")),
                _number(record.get("actual_closing_balance")),
                _number(record.get("variance")),
                status,
            ],
        )
        _style_status(ws.cell(row, 6), status)
        row += 1
    ws.freeze_panes = "A5"


def _write_account_proof(ws, summary: Mapping[str, Any], label: str) -> None:
    _write_title(
        ws,
        f"Reconciliation Proof: Each {('Cashbook' if label.casefold() == 'cash' else 'Bank')}, Month by Month",
        "Each selected account is rolled forward independently against its actual closing balance.",
        7,
    )
    headers = ["Account", "Month", "Net Movement", "Derived Closing", "Actual Ledger Closing", "Variance", "Status"]
    _write_table(ws, 3, headers, header=True)
    row = 4
    if not bool(summary.get("reconciliation_available")):
        _write_table(ws, row, ["Balance inputs not provided. Per-account proof is unavailable."])
        ws.cell(row, 1).fill = PatternFill("solid", fgColor=PALE_ORANGE)
        return
    frame = summary.get("reconciliation_by_account")
    labels = _account_labels(summary)
    if not _frame_has_rows(frame):
        _write_table(ws, row, ["Per-account proof data is unavailable"])
        return
    for record in frame.to_dict("records"):
        status = _safe_cell(record.get("status")) or "Not tested"
        _write_table(
            ws,
            row,
            [
                labels.get(_safe_cell(record.get("account_code")), _safe_cell(record.get("account_code"))),
                _month_label(record.get("month")),
                _number(record.get("net_movement")),
                _number(record.get("derived_closing")),
                _number(record.get("actual_closing_balance")),
                _number(record.get("variance")),
                status,
            ],
        )
        _style_status(ws.cell(row, 7), status)
        row += 1
    ws.freeze_panes = "A4"


def _write_title(ws, title: str, note: str, width: int) -> None:
    ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=max(width, 1))
    ws.cell(1, 1, _safe_cell(title))
    ws.cell(1, 1).font = Font(name="Arial", size=14, bold=True, color=NAVY)
    ws.cell(1, 1).alignment = Alignment(horizontal="left", vertical="center")
    ws.row_dimensions[1].height = 24
    ws.merge_cells(start_row=2, start_column=1, end_row=2, end_column=max(width, 1))
    ws.cell(2, 1, _safe_cell(note))
    ws.cell(2, 1).font = Font(name="Arial", size=9, italic=True, color=MUTED)
    ws.cell(2, 1).alignment = Alignment(wrap_text=False, vertical="center")
    ws.row_dimensions[2].height = 30


def _write_table(ws, row: int, values: list[Any], *, header: bool = False, total: bool = False) -> None:
    for column, value in enumerate(values, 1):
        cell = ws.cell(row, column, _safe_cell(value))
        cell.font = Font(name="Arial", size=10, bold=header or total, color=WHITE if header else TEXT)
        cell.alignment = Alignment(horizontal="center" if header else ("left" if column == 1 else "right"), vertical="center")
        if header:
            cell.fill = PatternFill("solid", fgColor=NAVY)
            cell.border = Border(bottom=Side(style="thin", color=WHITE))
        elif total:
            cell.fill = PatternFill("solid", fgColor=LIGHT_BLUE)
            cell.border = Border(top=Side(style="thin", color=NAVY))
        elif column > 1 and isinstance(cell.value, (int, float)):
            cell.number_format = MONEY_FORMAT
        if isinstance(cell.value, (pd.Timestamp,)):
            cell.number_format = DATE_FORMAT


def _style_cashflow_summary_values(ws, row: int, *, total_columns: list[int], percentage_column: int, local_total_column: int) -> None:
    _set_summary_font(ws.cell(row, 1), bold=True)
    for column in total_columns:
        _set_summary_font(ws.cell(row, column), color=EXCEL_BLUE, bold=True)
    _set_summary_font(ws.cell(row, percentage_column), color=EXCEL_BLUE)
    _set_summary_font(ws.cell(row, local_total_column), color=EXCEL_BROWN, bold=True)


def _set_summary_font(cell, *, color: str | None = None, bold: bool | None = None) -> None:
    font = copy(cell.font)
    if color is not None:
        font.color = color
    if bold is not None:
        font.bold = bold
    cell.font = font


def _style_section_band(ws, row: int, width: int) -> None:
    for column in range(1, width + 1):
        cell = ws.cell(row, column)
        cell.fill = PatternFill("solid", fgColor=LIGHT_BLUE)
        cell.font = Font(name="Arial", size=10, bold=True, color=NAVY)
        cell.border = Border(bottom=Side(style="thin", color=NAVY))


def _style_status(cell, status: Any) -> None:
    value = str(status or "").casefold()
    if value in {"matched", "confirmed"}:
        cell.fill = PatternFill("solid", fgColor=PALE_GREEN)
    elif value in {"variance", "not tested"} or value.startswith("review required"):
        cell.fill = PatternFill("solid", fgColor=PALE_ORANGE)
    elif value in {"failed", "error"}:
        cell.fill = PatternFill("solid", fgColor=PALE_RED)
    cell.font = Font(name="Arial", size=10, bold=True, color=TEXT)


def _finish_sheet(ws) -> None:
    ws.sheet_view.showGridLines = False
    for row in ws.iter_rows():
        for cell in row:
            if cell.value is not None and cell.font.name != "Arial":
                font = copy(cell.font)
                font.name = "Arial"
                cell.font = font
            if isinstance(cell.value, (int, float)):
                cell.number_format = MONEY_FORMAT
            alignment = copy(cell.alignment)
            alignment.vertical = "center"
            cell.alignment = alignment
    widths = {1: 28}
    for column_cells in ws.columns:
        index = column_cells[0].column
        length = max((len(str(cell.value)) if cell.value is not None else 0) for cell in column_cells)
        widths[index] = min(max(widths.get(index, 10), length + 2), 48)
    for index, width in widths.items():
        ws.column_dimensions[get_column_letter(index)].width = width


def _month_frame(frame: Any, months: list[str]) -> pd.DataFrame:
    if not isinstance(frame, pd.DataFrame) or frame.empty:
        return pd.DataFrame(columns=months, dtype=float)
    result = frame.copy()
    result.columns = [str(column) for column in result.columns]
    return result.reindex(columns=months, fill_value=0.0).fillna(0.0)


def _style_cashflow_section_band(ws, row: int, width: int) -> None:
    for column in range(1, width + 1):
        cell = ws.cell(row, column)
        cell.fill = PatternFill("solid", fgColor=PALE_GREEN)
        cell.font = Font(name="Arial", size=10, bold=True, color="416B4C")
        cell.border = Border(bottom=Side(style="thin", color="B8D8BE"))

def _frame_has_rows(frame: Any) -> bool:
    return isinstance(frame, pd.DataFrame) and not frame.empty


def _account_labels(summary: Mapping[str, Any]) -> dict[str, str]:
    labels: dict[str, str] = {}
    lines = summary.get("group_lines")
    if isinstance(lines, pd.DataFrame) and not lines.empty:
        for record in lines.to_dict("records"):
            code = _safe_cell(record.get("account_code"))
            name = _safe_cell(record.get("account_name"))
            if code and code not in labels:
                labels[code] = f"{name} [{code}]" if name else code
    return labels


def _month_range_label(months: Any, opening: bool = False) -> str:
    values = [str(month) for month in (months or [])]
    if not values:
        return "reporting period"
    if opening:
        first = values[0]
        return f"01-{pd.Period(first, freq='M').strftime('%b-%y')}"
    first = pd.Period(values[0], freq="M").strftime("%b-%y")
    last = pd.Period(values[-1], freq="M").strftime("%b-%y")
    return first if first == last else f"{first} to {last}"


def _month_label(value: Any) -> Any:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    try:
        return pd.Period(str(value), freq="M").strftime("%b-%y")
    except (TypeError, ValueError):
        return _safe_cell(value)


def _date_value(value: Any) -> Any:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    timestamp = pd.Timestamp(value)
    if pd.isna(timestamp):
        return None
    return timestamp.to_pydatetime()


def _number(value: Any) -> float | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return None if pd.isna(number) else number


def _safe_cell(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, (pd.Timestamp,)):
        return value.to_pydatetime()
    if isinstance(value, pd.Period):
        return str(value)
    if isinstance(value, float) and pd.isna(value):
        return None
    if isinstance(value, str):
        return _CONTROL_CHARS.sub("", value)
    if hasattr(value, "item"):
        try:
            return value.item()
        except ValueError:
            pass
    return value
