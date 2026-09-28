import pandas as pd
import pytest

from services.ledger_group_summary import (
    LedgerBalanceError,
    build_cashflow_summary,
    build_cross_group_check,
    build_group_summary,
)


def _fixture_data() -> tuple[pd.DataFrame, pd.DataFrame]:
    journal = pd.DataFrame(
        [
            # Customer receipt into the bank: both legs must be retained.
            ["V1", "2026-01-05", "B1", "Bank A", "Customer receipt", 100, 0],
            ["V1", "2026-01-05", "CUST", "Customer", "Customer receipt", 0, 100],
            # Expense payment from the bank.
            ["V2", "2026-01-12", "B1", "Bank A", "Expense payment", 0, 40],
            ["V2", "2026-01-12", "EXP", "Expense", "Expense payment", 40, 0],
            # A transfer inside the bank group: excluded from categories.
            ["V3", "2026-02-02", "B1", "Bank A", "Bank transfer", 50, 0],
            ["V3", "2026-02-02", "B2", "Bank B", "Bank transfer", 0, 50],
            # A contra account with no category: retained under its account name.
            ["V4", "2026-02-10", "B2", "Bank B", "Unmapped receipt", 30, 0],
            ["V4", "2026-02-10", "UNKNOWN", "Unmapped customer", "Unmapped receipt", 0, 30],
            # A bank-to-cash movement, surfaced separately as a related group.
            ["V5", "2026-02-15", "B1", "Bank A", "Cash funding", 0, 20],
            ["V5", "2026-02-15", "C1", "Cashbook", "Cash funding", 20, 0],
            # Same category with different casing; output should use one label.
            ["V6", "2026-02-20", "B1", "Bank A", "Charge", 0, 5],
            ["V6", "2026-02-20", "CHG", "Bank Charges", "Charge", 5, 0],
            ["V7", "2026-02-22", "B1", "Bank A", "Charge", 0, 7],
            ["V7", "2026-02-22", "CHG2", "Bank Charges", "Charge", 7, 0],
        ],
        columns=["voucher_id", "date", "account_code", "account_name", "voucher_detail", "debit", "credit"],
    )
    coa = pd.DataFrame(
        [
            ["B1", "Bank A", "Bank", "Deposits to Bank"],
            ["B2", "Bank B", "Bank", "Deposits to Bank"],
            ["C1", "Cashbook", "Cash", "Cash Balance"],
            ["CUST", "Customers", "Accounts Receivable", "Customers"],
            ["EXP", "Expense", "Expense", "Operating Expense"],
            ["CHG", "Bank Charges", "Expense", "Bank Charges and Taxes"],
            ["CHG2", "Bank Charges", "Expense", "BANK CHARGES AND TAXES"],
        ],
        columns=["account_code", "account_title", "account_type", "category"],
    )
    return journal, coa


def test_build_group_summary_uses_full_vouchers_and_balances_monthly():
    journal, coa = _fixture_data()
    result = build_group_summary(
        journal,
        coa,
        ["B1", "B2"],
        "2026-01-01",
        "2026-02-28",
        group_label="Bank",
        related_group_codes=["C1"],
        related_group_label="Bank",
        related_group_category="Cash Balance",
    )

    assert result["selected_vouchers"] == ["V1", "V2", "V3", "V4", "V5", "V6", "V7"]
    assert len(result["working_rows"]) == len(journal)
    assert result["monthly_balance_checks"]["balanced"].all()
    assert result["balance_checks"]["balanced"].all()

    categories = result["category_totals"]
    assert categories.loc["Customers", "2026-01"] == 100
    assert categories.loc["Operating Expense", "2026-01"] == -40
    assert categories.loc["Cash Balance", "2026-02"] == -20
    assert categories.loc["Bank Charges and Taxes", "Total"] == -12

    # The internal transfer is available for transparency but does not alter
    # the category totals.
    assert result["internal_transfers"]["voucher_id"].unique().tolist() == ["V3"]
    assert result["category_pivot"].loc[:, "2026-02"].sum() == -2
    assert result["group_account_pivot"].loc[:, "2026-02"].sum() == -2

    review = result["categories_needing_review"]
    assert review["account_code"].tolist() == ["UNKNOWN"]
    assert review["fallback_category"].tolist() == ["Unmapped customer"]


def test_cashflow_summary_buckets_transactions_by_own_date_and_zero_fills_periods():
    journal, coa = _fixture_data()

    result = build_cashflow_summary(
        journal,
        coa,
        ["C1"],
        ["B1", "B2"],
        "2026-01-01",
        "2026-03-31",
        group_by="month",
    )

    assert [period["key"] for period in result["periods"]] == ["2026-01", "2026-02", "2026-03"]
    categories = result["category_totals"]
    assert categories.loc["Customers", "2026-01"] == 100
    assert categories.loc["Unmapped customer", "2026-02"] == 30
    assert categories.loc["Customers", "2026-03"] == 0
    assert result["group_by"] == "month"


@pytest.mark.parametrize(
    ("grouping", "period_start", "period_end", "expected_keys"),
    [
        ("day", "2026-01-05", "2026-01-06", ["2026-01-05", "2026-01-06"]),
        ("fortnight", "2026-01-01", "2026-02-28", ["2026-01-1", "2026-01-2", "2026-02-1", "2026-02-2"]),
        ("quarter", "2026-02-10", "2026-04-18", ["2026-Q1", "2026-Q2"]),
        ("half_year", "2026-02-10", "2026-08-18", ["2026-H1", "2026-H2"]),
        ("year", "2026-02-10", "2027-01-05", ["2026", "2027"]),
    ],
)
def test_cashflow_summary_supports_all_phase_one_groupings(grouping, period_start, period_end, expected_keys):
    journal, coa = _fixture_data()

    result = build_cashflow_summary(
        journal,
        coa,
        ["C1"],
        ["B1", "B2"],
        period_start,
        period_end,
        group_by=grouping,
    )

    assert result["group_by"] == grouping
    assert [period["key"] for period in result["periods"]] == expected_keys
    assert result["periods"][0]["start"] == period_start
    assert result["periods"][-1]["end"] == period_end


def test_unbalanced_voucher_stops_report_generation():
    journal, coa = _fixture_data()
    journal.loc[len(journal)] = ["BAD", "2026-02-25", "B1", "Bank A", "Broken", 10, 0]

    with pytest.raises(LedgerBalanceError) as error:
        build_group_summary(journal, coa, ["B1", "B2"], "2026-01-01", "2026-02-28")

    assert not bool(error.value.checks.loc[error.value.checks["voucher_id"] == "BAD", "balanced"].iloc[0])


def test_reconciliation_proof_is_available_when_balances_are_supplied():
    journal, coa = _fixture_data()
    result = build_group_summary(
        journal,
        coa,
        ["B1", "B2"],
        "2026-01-01",
        "2026-02-28",
        opening_balances={"B1": 1000, "B2": 500},
        actual_closing_balances={
            "B1": {"2026-01": 1060, "2026-02": 1078},
            "B2": {"2026-01": 500, "2026-02": 480},
        },
    )

    assert result["reconciliation_available"] is True
    assert result["reconciliation_proof"]["status"].tolist() == ["Matched", "Matched"]
    assert result["reconciliation_proof"]["variance"].abs().max() == 0
    assert len(result["reconciliation_by_account"]) == 4


def test_cross_group_check_applies_the_requested_sign():
    journal, coa = _fixture_data()
    bank = build_group_summary(
        journal,
        coa,
        ["B1", "B2"],
        "2026-01-01",
        "2026-02-28",
        related_group_codes=["C1"],
        related_group_category="Cash Balance",
    )
    cash = build_group_summary(
        journal,
        coa,
        ["C1"],
        "2026-01-01",
        "2026-02-28",
        related_group_codes=["B1", "B2"],
        related_group_label="Bank",
        related_group_category="Received/Paid to Bank",
    )

    check = build_cross_group_check(
        cash,
        bank,
        "Received/Paid to Bank",
        "Cash Balance",
        right_multiplier=-1,
    )
    assert check["status"].tolist() == ["Matched", "Matched"]
    assert check["variance"].abs().max() == 0
