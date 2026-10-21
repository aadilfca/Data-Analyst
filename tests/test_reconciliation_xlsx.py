from pathlib import Path

import openpyxl
import pandas as pd
import pytest

from services.ledger_group_summary import build_cross_group_check, build_group_summary
from services.reconciliation_xlsx import (
    build_reconciliation_workbook,
    export_reconciliation_xlsx,
)


def _fixture_data() -> tuple[pd.DataFrame, pd.DataFrame]:
    journal = pd.DataFrame(
        [
            ["V1", "2026-01-05", "B1", "Bank A", "Customer receipt", 100, 0],
            ["V1", "2026-01-05", "CUST", "Customer", "Customer receipt", 0, 100],
            ["V2", "2026-01-12", "B1", "Bank A", "Expense payment", 0, 40],
            ["V2", "2026-01-12", "EXP", "Expense", "Expense payment", 40, 0],
            ["V3", "2026-02-02", "B1", "Bank A", "Bank transfer", 50, 0],
            ["V3", "2026-02-02", "B2", "Bank B", "Bank transfer", 0, 50],
            ["V4", "2026-02-10", "B2", "Bank B", "Unmapped receipt", 30, 0],
            ["V4", "2026-02-10", "UNKNOWN", "Unmapped customer", "Unmapped receipt", 0, 30],
            ["V5", "2026-02-15", "B1", "Bank A", "Cash funding", 0, 20],
            ["V5", "2026-02-15", "C1", "Cashbook", "Cash funding", 20, 0],
            ["V6", "2026-02-20", "B1", "Bank A", "Charge", 0, 5],
            ["V6", "2026-02-20", "CHG", "Bank Charges", "Charge", 5, 0],
        ],
        columns=["voucher_id", "date", "account_code", "account_name", "voucher_detail", "debit", "credit"],
    )
    coa = pd.DataFrame(
        [
            ["B1", "Bank A", "Bank", "Deposits to Bank"],
            ["B2", "Bank B", "Bank", "Deposits to Bank"],
            ["C1", "Cashbook", "Cash", "Cash Balance"],
            ["CUST", "Customers", "Receivable", "Customers"],
            ["EXP", "Expense", "Expense", "Operating Expense"],
            ["CHG", "Bank Charges", "Expense", "Bank Charges and Taxes"],
        ],
        columns=["account_code", "account_title", "account_type", "category"],
    )
    return journal, coa


def _summary(with_balances: bool = False):
    journal, coa = _fixture_data()
    return build_group_summary(
        journal,
        coa,
        ["B1", "B2"],
        "2026-01-01",
        "2026-02-28",
        group_label="Bank",
        related_group_codes=["C1"],
        related_group_label="Cash",
        related_group_category="Cash Balance",
        opening_balances={"B1": 1000, "B2": 500} if with_balances else None,
        actual_closing_balances=(
            {"B1": {"2026-01": 1060, "2026-02": 1078}, "B2": {"2026-01": 500, "2026-02": 480}}
            if with_balances
            else None
        ),
    )


def test_bank_export_keeps_nonzero_categories_and_professional_structure():
    summary = _summary()
    workbook = build_reconciliation_workbook(summary)

    assert workbook.sheetnames == [
        "Bank Monthly Summary",
        "Transfer Schedule",
        "Review Schedule",
        "Reconciliation Proof",
        "Reconciliation Proof - By Bank",
    ]
    sheet = workbook["Bank Monthly Summary"]
    assert sheet["A6"].value == "Customers"
    assert sheet["B6"].value == 100
    assert sheet["B6"].number_format == '#,##0;[Red](#,##0);-'
    labels = {sheet.cell(row, 1).value: row for row in range(1, sheet.max_row + 1)}
    assert any(str(value).startswith("Total BANK INFLOWS") for value in labels)
    assert labels["Operating Expense"] > 0
    assert sheet.cell(labels["Operating Expense"], 2).value == 40
    assert sheet.cell(labels["Net Movement (Inflows - Payments)"], 2).value == 60
    assert sheet["A1"].font.name == "Arial"
    assert sheet["A5"].fill.fgColor.rgb.endswith("1F4E78")
    assert sheet.freeze_panes == "B6"


def test_cash_export_includes_transfer_review_cross_check_and_unavailable_proof():
    summary = _summary()
    cash_summary = dict(summary)
    cash_summary["group_label"] = "Cash"
    cash_summary["group_account_codes"] = ["B1", "B2"]
    cross = build_cross_group_check(summary, summary, "Cash Balance", "Cash Balance")
    workbook = build_reconciliation_workbook(cash_summary, cross_group_check=cross)

    assert "Inter-Cashbook Transfers" in workbook.sheetnames
    assert "Cross-check vs Bank Summary" in workbook.sheetnames
    assert workbook["Reconciliation Proof"]["A5"].value.startswith("Balance inputs not provided")
    assert workbook["Reconciliation Proof"]["A5"].fill.fgColor.rgb.endswith("FCE4D6")
    assert workbook["Review Schedule"]["A5"].value in {"Jan-26", "Feb-26"}


def test_reconciliation_proofs_are_written_when_balances_are_available():
    summary = _summary(with_balances=True)
    workbook = build_reconciliation_workbook(summary, opening_balances={"B1": 1000, "B2": 500})

    combined = workbook["Reconciliation Proof"]
    assert combined["A5"].value == "Opening 01-Jan-26"
    assert combined["C5"].value == 1500
    assert combined["F6"].value == "Matched"
    assert workbook["Reconciliation Proof - By Bank"]["A4"].value == "Bank A [B1]"


def test_exported_xlsx_round_trips_as_typed_numbers(tmp_path):
    output = tmp_path / "bank_report.xlsx"
    output.write_bytes(export_reconciliation_xlsx(_summary()))

    workbook = openpyxl.load_workbook(output, data_only=True)
    sheet = workbook["Bank Monthly Summary"]
    assert isinstance(sheet["B6"].value, (int, float))
    assert sheet["B6"].value == 100
    assert workbook.calculation.calcMode == "auto"


def test_reference_workbooks_define_the_golden_tab_contract_when_present():
    bank_path = Path("uploads/Bank_Monthly_Summary v1.xlsx")
    cash_path = Path("uploads/Cash_Monthly_Summary.xlsx")
    if not bank_path.exists() or not cash_path.exists():
        return

    bank = openpyxl.load_workbook(bank_path, read_only=True, data_only=False)
    cash = openpyxl.load_workbook(cash_path, read_only=True, data_only=False)
    assert bank.sheetnames == ["Bank Monthly Summary", "Reconciliation Proof", "Reconciliation Proof - By Bank"]
    assert cash.sheetnames == [
        "Cash Monthly Summary",
        "Inter company",
        "Cross-check vs Bank Summary",
        "Inter-Cashbook Transfers",
        "Reconciliation Proof",
        "Reconciliation Proof - By Book",
    ]
    assert [bank["Bank Monthly Summary"].cell(5, column).value for column in range(1, 9)] == [
        "Category", "Jan-26", "Feb-26", "Mar-26", "Apr-26", "May-26", "Jun-26", "Total"
    ]
    assert [cash["Cash Monthly Summary"].cell(5, column).value for column in range(1, 9)] == [
        "Category", "Jan-26", "Feb-26", "Mar-26", "Apr-26", "May-26", "Jun-26", "Total"
    ]


def test_generated_golden_movements_match_reference_workbooks_when_source_is_present():
    source_path = Path("uploads/1. All Vouchers (Jan To June) - Copy.xlsx")
    bank_path = Path("uploads/Bank_Monthly_Summary v1.xlsx")
    cash_path = Path("uploads/Cash_Monthly_Summary.xlsx")
    if not source_path.exists() or not bank_path.exists() or not cash_path.exists():
        return

    journal = pd.read_excel(source_path, sheet_name="All vouchers").rename(
        columns={
            "Voucher ID": "voucher_id",
            "Date": "date",
            "Account Code": "account_code",
            "Account Name": "account_name",
            "Voucher Detail": "voucher_detail",
            "Debit": "debit",
            "Credit": "credit",
        }
    )
    coa = pd.read_excel(source_path, sheet_name="Categories assigned").rename(
        columns={"Account #": "account_code", "Account Title": "account_title", "Account Type": "account_type", "Cat": "category"}
    )
    bank = build_group_summary(
        journal,
        coa,
        [
            "10-15-115-15-0001",
            "10-15-115-15-0002",
            "10-15-115-15-0003",
            "10-15-115-15-0004",
            "10-15-115-15-0005",
            "10-15-115-15-0006",
        ],
        "2026-01-01",
        "2026-06-30",
        group_label="Bank",
        related_group_codes=["10-15-110-0001", "10-15-115-10-0001", "10-15-115-10-0003"],
        related_group_category="Cash Balance",
    )
    cash = build_group_summary(
        journal,
        coa,
        ["10-15-110-0001", "10-15-115-10-0001", "10-15-115-10-0003"],
        "2026-01-01",
        "2026-06-30",
        group_label="Cash",
        related_group_codes=[
            "10-15-115-15-0001",
            "10-15-115-15-0002",
            "10-15-115-15-0003",
            "10-15-115-15-0004",
            "10-15-115-15-0005",
            "10-15-115-15-0006",
        ],
        related_group_category="Received/Paid to Bank",
    )

    def net_values(summary):
        workbook = build_reconciliation_workbook(summary)
        sheet = workbook[f"{summary['group_label']} Monthly Summary"]
        row = next(row for row in range(1, sheet.max_row + 1) if sheet.cell(row, 1).value == "Net Movement (Inflows - Payments)")
        return [sheet.cell(row, column).value for column in range(2, 8)]

    bank_reference = openpyxl.load_workbook(bank_path, data_only=True)
    cash_reference = openpyxl.load_workbook(cash_path, data_only=True)
    expected_bank = [bank_reference["Bank Monthly Summary"].cell(29, column).value for column in range(2, 8)]
    expected_cash = [cash_reference["Cash Monthly Summary"].cell(37, column).value for column in range(2, 8)]
    assert net_values(bank) == pytest.approx(expected_bank)
    assert net_values(cash) == pytest.approx(expected_cash)


def test_signed_party_export_keeps_reversals_and_negative_net_as_numbers():
    from services.reconciliation_xlsx import build_cashflow_workbook
    preview = dict(periods=[dict(key='2026-01', label='Jan-26')], category_totals=[],
        customer_payment_account_period_totals=[dict(period_label='Jan-26', customer_account_name='Customer',
            customer_account_code='AR', customer_receipts=100, customer_reversals=140,
            customer_gross_amount=-40, cashflow_amount=-40)],
        vendor_payment_period_totals=[dict(period_label='Jan-26', account_name='Vendor', account_code='AP',
            category='Suppliers', payments=90, reversals=120, net_payments=-30)])
    workbook = build_cashflow_workbook(preview)
    customers = [r for r in workbook['Customer Period Totals'].iter_rows(values_only=True) if 'Customer' in r]
    vendors = [r for r in workbook['Vendor Period Totals'].iter_rows(values_only=True) if 'Vendor' in r]
    assert any(100 in r and 140 in r and -40 in r for r in customers)
    assert any(90 in r and 120 in r and -30 in r for r in vendors)
