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
from copy import copy
from typing import Any, Mapping

import pandas as pd
from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
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

MONEY_FORMAT = '#,##0.00;[Red](#,##0.00);-'
DATE_FORMAT = "dd-mmm-yy"

_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


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
    _write_cashflow_summary_sheet(
        summary_sheet,
        preview,
        period_keys,
        period_labels,
        period_start=period_start,
        period_end=period_end,
        group_by=group_by,
    )
    _write_cashflow_detail_sheet(
        workbook.create_sheet("Internal Transfers"),
        "Internal Transfers",
        preview.get("transfer_details") or [],
        [("voucher_id", "Voucher", False), ("period", "Period", False), ("transfer_type", "Transfer", False), ("amount", "Amount", True), ("matched", "Matched", False)],
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
        preview.get("categories_needing_review") or [],
        [("voucher_id", "Voucher", False), ("period", "Period", False), ("account_code", "Account code", False), ("account_name", "Account", False), ("fallback_category", "Review category", False)],
    )

    controls = workbook.create_sheet("Export Controls")
    _write_title(controls, "Cashflows Export Controls", "Control totals and selection used for this workbook.", 2)
    control_rows = [
        ("Period start", period_start or ""),
        ("Period end", period_end or ""),
        ("Grouping", group_by or preview.get("group_by") or "month"),
        ("Receipt presentation", preview.get("receipt_presentation") or "net"),
        ("Selected vouchers", int(preview.get("selected_voucher_count") or 0)),
        ("Internal transfers", int(preview.get("internal_transfer_count") or 0)),
        ("Customer deductions", int(preview.get("deduction_count") or 0)),
        ("Review required", int(preview.get("category_review_count") or 0)),
    ]
    row = 4
    _write_table(controls, row, ["Control", "Value"], header=True)
    for control, value in control_rows:
        row += 1
        _write_table(controls, row, [control, value])

    for sheet in workbook.worksheets:
        _finish_sheet(sheet)
    return workbook


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
        f"Visible Cashflows report for {period_start or 'selected start'} to {period_end or 'selected end'}; grouping: {group_by or preview.get('group_by') or 'month'}.",
        len(period_keys) + 2,
    )
    row = 4
    _write_table(ws, row, ["Category", *period_labels, "Total"], header=True)
    records = preview.get("category_totals") or []
    for record in records:
        row += 1
        values = [_number(record.get(key)) or 0.0 for key in period_keys]
        total = _number(record.get("Total"))
        _write_table(ws, row, [record.get("category") or "Unlabelled", *values, total if total is not None else sum(values)])
    if not records:
        row += 1
        _write_table(ws, row, ["No Cash Flow activity was found."])
    else:
        row += 1
        totals = [sum(_number(record.get(key)) or 0.0 for record in records) for key in period_keys]
        _write_table(ws, row, ["Total", *totals, sum(totals)], total=True)
    ws.freeze_panes = "B5"


def _write_cashflow_detail_sheet(ws, title: str, records: list[Mapping[str, Any]], columns: list[tuple[str, str, bool]]) -> None:
    _write_title(ws, title, "Detail rows included in the selected Cashflows report.", len(columns))
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
            if key == "matched":
                value = "Yes" if value else "Shown separately"
            values.append(_number(value) if numeric else value)
        _write_table(ws, row, values)
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


def _style_section_band(ws, row: int, width: int) -> None:
    for column in range(1, width + 1):
        cell = ws.cell(row, column)
        cell.fill = PatternFill("solid", fgColor=LIGHT_BLUE)
        cell.font = Font(name="Arial", size=10, bold=True, color=NAVY)
        cell.border = Border(bottom=Side(style="thin", color=NAVY))


def _style_status(cell, status: Any) -> None:
    value = str(status or "").casefold()
    if value == "matched":
        cell.fill = PatternFill("solid", fgColor=PALE_GREEN)
    elif value in {"variance", "not tested"}:
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
