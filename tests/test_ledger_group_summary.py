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

def test_cashflow_summary_uses_prepared_month_and_fortnight_keys_without_changing_totals():
    journal, coa = _fixture_data()
    parsed_dates = pd.to_datetime(journal["date"])
    journal["transaction_month"] = parsed_dates.dt.strftime("%Y-%m")
    journal["transaction_fortnight"] = (
        parsed_dates.dt.strftime("%Y-%m")
        + "-F"
        + parsed_dates.dt.day.map(lambda day: "1" if day <= 14 else "2")
    )

    prepared_months = build_cashflow_summary(
        journal, coa, ["C1"], ["B1", "B2"], "2026-01-01", "2026-03-31", group_by="month"
    )
    prepared_fortnights = build_cashflow_summary(
        journal, coa, ["C1"], ["B1", "B2"], "2026-01-01", "2026-02-28", group_by="fortnight"
    )
    baseline = journal.drop(columns=["transaction_month", "transaction_fortnight"])
    baseline_months = build_cashflow_summary(
        baseline, coa, ["C1"], ["B1", "B2"], "2026-01-01", "2026-03-31", group_by="month"
    )
    baseline_fortnights = build_cashflow_summary(
        baseline, coa, ["C1"], ["B1", "B2"], "2026-01-01", "2026-02-28", group_by="fortnight"
    )

    pd.testing.assert_frame_equal(prepared_months["category_totals"], baseline_months["category_totals"])
    pd.testing.assert_frame_equal(prepared_fortnights["category_totals"], baseline_fortnights["category_totals"])

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



@pytest.mark.parametrize(
    ("grouping", "period_start", "period_end"),
    [
        ("day", "2026-01-05", "2026-02-22"),
        ("fortnight", "2026-01-01", "2026-02-28"),
        ("month", "2026-01-01", "2026-03-31"),
        ("quarter", "2026-02-10", "2026-04-18"),
        ("half_year", "2026-02-10", "2026-08-18"),
        ("year", "2026-02-10", "2027-01-05"),
    ],
)
def test_cashflow_drilldown_reconciles_summary_across_all_groupings(grouping, period_start, period_end):
    journal, coa = _fixture_data()
    result = build_cashflow_summary(
        journal,
        coa,
        ["C1"],
        ["B1", "B2"],
        period_start,
        period_end,
        group_by=grouping,
        include_drilldown=True,
    )

    category_totals = result["category_totals"].drop(columns=["Total"])
    rollup_totals = result["cashflow_ledger_rollups"].groupby(["category", "period_key"])["amount"].sum()
    for category in category_totals.index:
        for period_key in category_totals.columns:
            detail_total = rollup_totals.get((str(category), str(period_key)), 0.0)
            assert detail_total == pytest.approx(float(category_totals.loc[category, period_key]))

    controls = result["cashflow_drilldown_controls"]
    assert all(row["status"] == "Matched" for row in controls["category_controls"])
    split = controls["cash_bank_split"]
    assert split["detail_total"] == pytest.approx(float(result["cashflow_ledger_rollups"]["amount"].sum()))
    assert split["status"] == "Matched"
    assert result["periods"][0]["start"] == period_start
    assert result["periods"][-1]["end"] == period_end


def test_cashflow_summary_clips_boundaries_and_zero_fills_empty_periods_with_drilldown():
    journal, coa = _fixture_data()
    result = build_cashflow_summary(
        journal,
        coa,
        ["C1"],
        ["B1", "B2"],
        "2026-01-10",
        "2026-03-10",
        group_by="month",
        include_drilldown=True,
    )

    assert [(period["key"], period["start"], period["end"]) for period in result["periods"]] == [
        ("2026-01", "2026-01-10", "2026-01-31"),
        ("2026-02", "2026-02-01", "2026-02-28"),
        ("2026-03", "2026-03-01", "2026-03-10"),
    ]
    assert (result["category_totals"]["2026-03"] == 0).all()
    march_controls = [
        row for row in result["cashflow_drilldown_controls"]["category_controls"]
        if row["period_key"] == "2026-03"
    ]
    assert march_controls
    assert all(row["status"] == "Matched" and row["summary_total"] == 0 and row["detail_total"] == 0 for row in march_controls)


def test_cashflow_summary_nets_customer_receipt_and_keeps_deduction_detail():
    journal = pd.DataFrame(
        [
            ["V1", "2026-01-05", "B1", "Bank A", "Customer receipt", 110, 0],
            ["V1", "2026-01-05", "CUST", "Customer", "Customer receipt", 0, 100],
            ["V1", "2026-01-05", "CHG", "Bank charge", "Customer receipt", 0, 10],
        ],
        columns=["voucher_id", "date", "account_code", "account_name", "voucher_detail", "debit", "credit"],
    )
    coa = pd.DataFrame(
        [
            ["B1", "Bank A", "Bank", "Bank"],
            ["CUST", "Customer", "Receivable", "Customer receipts"],
            ["CHG", "Bank charge", "Expense", "Bank charges"],
        ],
        columns=["account_code", "account_title", "account_type", "category"],
    )

    result = build_cashflow_summary(
        journal,
        coa,
        [],
        ["B1"],
        "2026-01-01",
        "2026-01-31",
        receipt_presentation="net",
    )

    assert result["category_totals"].loc["Customer receipts", "2026-01"] == 110
    assert result["deduction_details"]["presentation"].tolist() == ["netted_from_customer_receipt"]
    assert result["deduction_details"]["amount"].tolist() == [10.0]


def test_configured_customer_roles_classify_other_categorized_lines_as_other_deduction():
    journal = pd.DataFrame(
        [
            ["V1", "2026-01-05", "BANK", "Export bank", "Gross receipt", 100, 7],
            ["V1", "2026-01-05", "CUSTOMER", "Export customer", "Gross receipt", 0, 100],
            ["V1", "2026-01-05", "CHARGE", "Bank charge", "Gross receipt", 1, 0],
            ["V1", "2026-01-05", "WHT", "Withholding tax", "Gross receipt", 2, 0],
            ["V1", "2026-01-05", "OTHER", "Export service deduction", "Gross receipt", 4, 0],
        ],
        columns=["voucher_id", "date", "account_code", "account_name", "voucher_detail", "debit", "credit"],
    )
    coa = pd.DataFrame(
        [
            ["BANK", "Export bank", "Bank", "Bank"],
            ["CUSTOMER", "Export customer", "Receivable", "Received from customers"],
            ["CHARGE", "Bank charge", "Expense", "Bank charges"],
            ["WHT", "Withholding tax", "Tax", "Withholding tax on export"],
            ["OTHER", "Export service deduction", "Expense", "Export service expense"],
        ],
        columns=["account_code", "account_title", "account_type", "category"],
    )

    result = build_cashflow_summary(
        journal,
        coa,
        [],
        ["BANK"],
        "2026-01-01",
        "2026-01-31",
        cashflow_role_categories={
            "customer_receipt": ["Received from customers"],
            "bank_charge": ["Bank charges"],
            "withholding_tax": ["Withholding tax on export"],
            "other_deduction": [],
        },
    )

    detail = result["customer_payment_details"].iloc[0]
    assert detail["bank_charges"] == 1
    assert detail["withholding_tax"] == 2
    assert detail["other_deductions"] == 4
    assert detail["expected_net_settlement"] == 93
    assert detail["variance"] == 0
    assert detail["status"] == "Matched"


def test_customer_voucher_residual_debits_are_deductions_and_credits_need_review():
    journal = pd.DataFrame(
        [
            ["V1", "2026-01-05", "BANK", "Export bank", "Customer receipt", 100, 0],
            ["V1", "2026-01-05", "CUSTOMER", "Export customer", "Customer receipt", 0, 100],
            ["V1", "2026-01-05", "UNMAPPED-DEBIT", "Unmapped debit counter", "Customer receipt", 2, 0],
            ["V1", "2026-01-05", "RESIDUAL-CREDIT", "Residual credit", "Customer receipt", 0, 2],
        ],
        columns=["voucher_id", "date", "account_code", "account_name", "voucher_detail", "debit", "credit"],
    )
    coa = pd.DataFrame(
        [
            ["BANK", "Export bank", "Bank", ""],
            ["CUSTOMER", "Export customer", "Receivable", "Received from customers"],
            ["RESIDUAL-CREDIT", "Residual credit", "Receivable", "Unconfigured receipt credit"],
        ],
        columns=["account_code", "account_title", "account_type", "category"],
    )

    result = build_cashflow_summary(
        journal,
        coa,
        cash_account_codes=[],
        bank_account_codes=["BANK"],
        period_start="2026-01-01",
        period_end="2026-01-31",
        cashflow_role_categories={
            "customer_receipt": ["Received from customers"],
            "bank_charge": [],
            "withholding_tax": [],
            "other_deduction": [],
        },
    )

    detail = result["customer_payment_details"].iloc[0]
    assert detail["other_deductions"] == 2
    assert result["deduction_details"]["account_code"].tolist() == ["UNMAPPED-DEBIT"]
    assert result["categories_needing_review"]["account_code"].tolist() == ["RESIDUAL-CREDIT"]
    assert result["category_totals"].empty


def test_configured_customer_role_identifies_cash_and_bank_with_client_specific_labels():
    journal = pd.DataFrame(
        [
            ["CASH-CLIENT", "2026-01-05", "CASH", "Cash in hand", "Cash receipt", 50, 0],
            ["CASH-CLIENT", "2026-01-05", "CUST-C", "Overseas customer cash", "Cash receipt", 0, 50],
            ["BANK-CLIENT", "2026-01-06", "BANK", "Export bank", "Bank receipt", 75, 0],
            ["BANK-CLIENT", "2026-01-06", "CUST-B", "Overseas customer bank", "Bank receipt", 0, 75],
            ["UNMAPPED", "2026-01-07", "BANK", "Export bank", "Unmapped receipt", 10, 0],
            ["UNMAPPED", "2026-01-07", "LOOKS-LIKE-CUSTOMER", "Customer-looking account", "Unmapped receipt", 0, 10],
        ],
        columns=["voucher_id", "date", "account_code", "account_name", "voucher_detail", "debit", "credit"],
    )
    coa = pd.DataFrame(
        [
            ["CASH", "Cash in hand", "Cash", ""],
            ["BANK", "Export bank", "Bank", ""],
            ["CUST-C", "Overseas customer cash", "Receivable", "Export customer collections"],
            ["CUST-B", "Overseas customer bank", "Receivable", "Export customer collections"],
            ["LOOKS-LIKE-CUSTOMER", "Customer-looking account", "Receivable", "Other income"],
        ],
        columns=["account_code", "account_title", "account_type", "category"],
    )

    result = build_cashflow_summary(
        journal,
        coa,
        cash_account_codes=["CASH"],
        bank_account_codes=["BANK"],
        period_start="2026-01-01",
        period_end="2026-01-31",
        cashflow_role_categories={
            "customer_receipt": ["Export customer collections"],
            "bank_charge": [],
            "withholding_tax": [],
            "other_deduction": [],
        },
    )

    details = result["customer_payment_details"].set_index("voucher_id")
    assert set(details.index) == {"CASH-CLIENT", "BANK-CLIENT"}
    assert details.loc["CASH-CLIENT", "bank_account_type"] == "Cash"
    assert details.loc["BANK-CLIENT", "bank_account_type"] == "Bank"
    assert details["status"].eq("Matched").all()
    assert "UNMAPPED" not in details.index


def test_customer_payment_reconciliation_separates_technical_bank_credit_from_deductions():
    journal = pd.DataFrame(
        [
            ["GROSS", "2026-01-05", "BANK", "Export bank", "Gross receipt", 100, 2],
            ["GROSS", "2026-01-05", "BANK", "Export bank", "Gross receipt", 0, 1],
            ["GROSS", "2026-01-05", "CUSTOMER-G", "Customer gross", "Gross receipt", 0, 100],
            ["GROSS", "2026-01-05", "CHARGE", "Bank charge", "Gross receipt", 1, 0],
            ["GROSS", "2026-01-05", "WHT", "Withholding tax", "Gross receipt", 2, 0],
            ["NET", "2026-01-06", "BANK", "Export bank", "Net receipt", 97, 0],
            ["NET", "2026-01-06", "CUSTOMER-N", "Customer net", "Net receipt", 0, 100],
            ["NET", "2026-01-06", "CHARGE", "Bank charge", "Net receipt", 1, 0],
            ["NET", "2026-01-06", "WHT", "Withholding tax", "Net receipt", 2, 0],
            ["CASH", "2026-01-07", "CASH", "Cash in hand", "Cash receipt", 50, 0],
            ["CASH", "2026-01-07", "CUSTOMER-C", "Customer cash", "Cash receipt", 0, 50],
        ],
        columns=["voucher_id", "date", "account_code", "account_name", "voucher_detail", "debit", "credit"],
    )
    coa = pd.DataFrame(
        [
            ["BANK", "Export bank", "Bank", ""],
            ["CASH", "Cash in hand", "Cash", ""],
            ["CUSTOMER-G", "Customer gross", "Receivable", "Customer receipts"],
            ["CUSTOMER-N", "Customer net", "Receivable", "Customer receipts"],
            ["CUSTOMER-C", "Customer cash", "Receivable", "Customer receipts"],
            ["CHARGE", "Bank charge", "Expense", "Bank charges"],
            ["WHT", "Withholding tax", "Tax", "Withholding tax"],
        ],
        columns=["account_code", "account_title", "account_type", "category"],
    )

    result = build_cashflow_summary(
        journal,
        coa,
        cash_account_codes=["CASH"],
        bank_account_codes=["BANK"],
        period_start="2026-01-01",
        period_end="2026-01-31",
        cashflow_role_categories={
            "customer_receipt": ["Customer receipts"],
            "bank_charge": ["Bank charges"],
            "withholding_tax": ["Withholding tax"],
            "other_deduction": [],
        },
        include_drilldown=True,
    )

    details = result["customer_payment_details"].set_index("voucher_id")
    assert details.loc["GROSS", "method"] == "Gross"
    assert details.loc["GROSS", "technical_bank_offset"] == 3
    assert details.loc["GROSS", "net_bank_settlement"] == 97
    assert details.loc["GROSS", "expected_net_settlement"] == 97
    assert details.loc["GROSS", "deductions_total"] == 3
    assert details.loc["NET", "method"] == "Net"
    assert details.loc["NET", "technical_bank_offset"] == 0
    assert details.loc["NET", "status"] == "Matched"
    assert details.loc["CASH", "bank_account_type"] == "Cash"
    assert details.loc["CASH", "technical_bank_offset"] == 0
    assert details.loc["CASH", "method"] == "Net"
    assert result["deduction_details"]["account_code"].tolist() == ["CHARGE", "WHT", "CHARGE", "WHT"]
    assert result["category_totals"].loc["Customer receipts", "Total"] == 244
    customer_rollups = result["cashflow_ledger_rollups"].loc[
        result["cashflow_ledger_rollups"]["category"] == "Customer receipts"
    ]
    assert set(customer_rollups["account_code"]) == {"__customer_payment__"}
    assert set(customer_rollups["account_name"]) == {"Customer payment (net bank receipt)"}


def test_customer_payment_ordinary_bank_charges_split_from_general_charges():
    journal = pd.DataFrame(
        [
            ["CUSTOMER", "2026-01-05", "BANK", "Export bank", "Customer receipt", 100, 3],
            ["CUSTOMER", "2026-01-05", "CUSTOMER", "Export customer", "Customer receipt", 0, 100],
            ["CUSTOMER", "2026-01-05", "OBC", "Ordinary bank charges", "Customer receipt", 3, 0],
            ["GENERAL", "2026-01-06", "BANK", "Export bank", "General bank charge", 0, 10],
            ["GENERAL", "2026-01-06", "OBC", "Ordinary bank charges", "General bank charge", 10, 0],
        ],
        columns=["voucher_id", "date", "account_code", "account_name", "voucher_detail", "debit", "credit"],
    )
    coa = pd.DataFrame(
        [
            ["BANK", "Export bank", "Bank", ""],
            ["CUSTOMER", "Export customer", "Receivable", "Customer receipts"],
            ["OBC", "Ordinary bank charges", "Expense", "Bank charges"],
        ],
        columns=["account_code", "account_title", "account_type", "category"],
    )

    result = build_cashflow_summary(
        journal,
        coa,
        cash_account_codes=[],
        bank_account_codes=["BANK"],
        period_start="2026-01-01",
        period_end="2026-01-31",
        include_drilldown=True,
        cashflow_role_categories={
            "customer_receipt": ["Customer receipts"],
            "bank_charge": ["Bank charges"],
            "withholding_tax": [],
            "other_deduction": [],
        },
    )

    payment = result["customer_payment_details"].iloc[0]
    assert payment["bank_charges"] == 3
    assert payment["net_bank_settlement"] == 97
    assert payment["status"] == "Matched"
    assert result["category_totals"].loc["Customer receipts", "Total"] == 97
    assert result["category_totals"].loc["Bank charges", "Total"] == -10
    assert result["deduction_details"]["presentation"].tolist() == [
        "netted_from_customer_receipt",
        "shown_separately",
    ]
    customer_rollups = result["cashflow_ledger_rollups"].loc[
        result["cashflow_ledger_rollups"]["category"] == "Customer receipts"
    ]
    assert set(customer_rollups["account_name"]) == {"Customer payment (net bank receipt)"}
    general_charge = result["cashflow_ledger_rollups"].loc[
        result["cashflow_ledger_rollups"]["category"] == "Bank charges"
    ]
    assert general_charge["account_code"].tolist() == ["OBC"]
    assert general_charge["amount"].tolist() == [-10.0]


def test_unmatched_customer_payment_is_excluded_from_net_cashflow_statement():
    journal = pd.DataFrame(
        [
            ["MULTI", "2026-01-05", "BANK", "Export bank", "Combined receipt", 100, 0],
            ["MULTI", "2026-01-05", "CUSTOMER-1", "Customer one", "Combined receipt", 0, 60],
            ["MULTI", "2026-01-05", "CUSTOMER-2", "Customer two", "Combined receipt", 0, 40],
        ],
        columns=["voucher_id", "date", "account_code", "account_name", "voucher_detail", "debit", "credit"],
    )
    coa = pd.DataFrame(
        [
            ["BANK", "Export bank", "Bank", ""],
            ["CUSTOMER-1", "Customer one", "Receivable", "Customer receipts"],
            ["CUSTOMER-2", "Customer two", "Receivable", "Customer receipts"],
        ],
        columns=["account_code", "account_title", "account_type", "category"],
    )

    result = build_cashflow_summary(
        journal,
        coa,
        cash_account_codes=[],
        bank_account_codes=["BANK"],
        period_start="2026-01-01",
        period_end="2026-01-31",
        cashflow_role_categories={
            "customer_receipt": ["Customer receipts"],
            "bank_charge": [],
            "withholding_tax": [],
            "other_deduction": [],
        },
    )

    assert result["customer_payment_details"].iloc[0]["status"] == "Review required"
    assert "Customer receipts" not in result["category_totals"].index
    assert result["cashflow_ledger_rollups"].empty

def test_explicit_other_deduction_role_reconciles_customer_payment():
    journal = pd.DataFrame(
        [
            ["OTHER-ROLE", "2026-01-08", "BANK", "Export bank", "Customer receipt", 90, 0],
            ["OTHER-ROLE", "2026-01-08", "CUSTOMER", "Export customer", "Customer receipt", 0, 100],
            ["OTHER-ROLE", "2026-01-08", "OTHER", "Export deduction", "Customer receipt", 10, 0],
        ],
        columns=["voucher_id", "date", "account_code", "account_name", "voucher_detail", "debit", "credit"],
    )
    coa = pd.DataFrame(
        [
            ["BANK", "Export bank", "Bank", ""],
            ["CUSTOMER", "Export customer", "Receivable", "Customer receipts"],
            ["OTHER", "Export deduction", "Expense", "Export deductions"],
        ],
        columns=["account_code", "account_title", "account_type", "category"],
    )

    result = build_cashflow_summary(
        journal,
        coa,
        cash_account_codes=[],
        bank_account_codes=["BANK"],
        period_start="2026-01-01",
        period_end="2026-01-31",
        cashflow_role_categories={
            "customer_receipt": ["Customer receipts"],
            "bank_charge": [],
            "withholding_tax": [],
            "other_deduction": ["Export deductions"],
        },
    )

    detail = result["customer_payment_details"].iloc[0]
    assert detail["other_deductions"] == 10
    assert detail["expected_net_settlement"] == 90
    assert detail["status"] == "Matched"
    assert result["categories_needing_review"].empty


def test_missing_customer_category_stays_out_of_customer_payments_and_needs_review():
    journal = pd.DataFrame(
        [
            ["MISSING-CUSTOMER", "2026-01-09", "BANK", "Export bank", "Receipt", 50, 0],
            ["MISSING-CUSTOMER", "2026-01-09", "CUSTOMER", "Foreign customer", "Receipt", 0, 50],
        ],
        columns=["voucher_id", "date", "account_code", "account_name", "voucher_detail", "debit", "credit"],
    )
    coa = pd.DataFrame(
        [
            ["BANK", "Export bank", "Bank", ""],
            ["CUSTOMER", "Foreign customer", "Receivable", ""],
        ],
        columns=["account_code", "account_title", "account_type", "category"],
    )

    result = build_cashflow_summary(
        journal,
        coa,
        cash_account_codes=[],
        bank_account_codes=["BANK"],
        period_start="2026-01-01",
        period_end="2026-01-31",
        cashflow_role_categories={
            "customer_receipt": ["Customer receipts"],
            "bank_charge": [],
            "withholding_tax": [],
            "other_deduction": [],
        },
    )

    assert result["customer_payment_details"].empty
    assert result["categories_needing_review"]["account_code"].tolist() == ["CUSTOMER"]


def test_multiple_receiving_accounts_require_customer_payment_review():
    journal = pd.DataFrame(
        [
            ["MULTI-RECEIVING", "2026-01-10", "BANK-1", "Export bank one", "Receipt", 60, 0],
            ["MULTI-RECEIVING", "2026-01-10", "BANK-2", "Export bank two", "Receipt", 40, 0],
            ["MULTI-RECEIVING", "2026-01-10", "CUSTOMER", "Export customer", "Receipt", 0, 100],
        ],
        columns=["voucher_id", "date", "account_code", "account_name", "voucher_detail", "debit", "credit"],
    )
    coa = pd.DataFrame(
        [
            ["BANK-1", "Export bank one", "Bank", ""],
            ["BANK-2", "Export bank two", "Bank", ""],
            ["CUSTOMER", "Export customer", "Receivable", "Customer receipts"],
        ],
        columns=["account_code", "account_title", "account_type", "category"],
    )

    result = build_cashflow_summary(
        journal,
        coa,
        cash_account_codes=[],
        bank_account_codes=["BANK-1", "BANK-2"],
        period_start="2026-01-01",
        period_end="2026-01-31",
        cashflow_role_categories={
            "customer_receipt": ["Customer receipts"],
            "bank_charge": [],
            "withholding_tax": [],
            "other_deduction": [],
        },
    )

    detail = result["customer_payment_details"].iloc[0]
    assert detail["bank_account_name"] == "Export bank one / Export bank two"
    assert detail["method"] == "Review"
    assert detail["status"] == "Review required"


def test_cashflow_summary_exposes_period_metrics_for_prepared_snapshot():
    journal, coa = _fixture_data()
    result = build_cashflow_summary(
        journal,
        coa,
        [],
        ["B1", "B2"],
        "2026-01-01",
        "2026-02-28",
        group_by="day",
    )

    assert result["period_metrics"]["2026-01-05"]["source_line_count"] == 2
    assert result["period_metrics"]["2026-01-05"]["selected_voucher_count"] == 1
    assert result["period_metrics"]["2026-02-10"]["category_review_count"] == 1

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



def test_reconciliation_proof_accumulates_partial_period_closings():
    journal, coa = _fixture_data()
    result = build_group_summary(
        journal,
        coa,
        ["B1", "B2"],
        "2026-01-10",
        "2026-02-15",
        opening_balances={"B1": 1000, "B2": 500},
        actual_closing_balances={
            "B1": {"2026-01": 960, "2026-02": 990},
            "B2": {"2026-01": 500, "2026-02": 480},
        },
    )

    proof = result["reconciliation_proof"]
    assert result["months"] == ["2026-01", "2026-02"]
    assert proof["derived_closing"].tolist() == [1460.0, 1470.0]
    assert proof["actual_closing_balance"].tolist() == [1460.0, 1470.0]
    assert proof["status"].tolist() == ["Matched", "Matched"]
    assert result["reconciliation_by_account"]["status"].eq("Matched").all()


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
