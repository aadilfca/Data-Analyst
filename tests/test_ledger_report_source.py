import pandas as pd

from services.ledger_report_source import (
    build_report_source,
    load_ledger_report_source,
)


def _journal_rows():
    return [
        {
            "client_data_id": 1,
            "template_id": 9,
            "upload_id": 12,
            "row_data": {
                "Voucher ID": "V1",
                "Date": "2026-01-05",
                "Account Code": "B1",
                "Account Name": "Bank A",
                "Voucher Detail": "Receipt",
                "Debit": 100,
                "Credit": 0,
            },
        },
        {
            "client_data_id": 2,
            "template_id": 9,
            "upload_id": 12,
            "row_data": {
                "Voucher ID": "V1",
                "Date": "2026-01-05",
                "Account Code": "C1",
                "Account Name": "Customer",
                "Voucher Detail": "Receipt",
                "Debit": 0,
                "Credit": 100,
            },
        },
    ]


def test_build_report_source_maps_client_data_and_coa_aliases():
    source = build_report_source(
        _journal_rows(),
        [
            {
                "account_code": "B1",
                "account_name": "Bank A",
                "source_account_type": "Bank",
                "category_name": "Bank Balance",
            },
            {
                "account_code": "C1",
                "account_name": "Customer",
                "source_account_type": "Receivable",
                "category_name": "Customers",
            },
        ],
        period_start="2026-01-01",
        period_end="2026-01-31",
    )

    assert source.ready
    assert list(source.journal.columns) == [
        "voucher_id", "date", "account_code", "account_name",
        "voucher_detail", "debit", "credit",
    ]
    assert source.journal.loc[0, "account_code"] == "B1"
    assert source.journal.loc[0, "debit"] == 100.0
    assert source.coa.set_index("account_code").loc["C1", "category"] == "Customers"
    assert source.journal["date"].iloc[0] == pd.Timestamp("2026-01-05")


def test_build_report_source_returns_blocking_diagnostics_for_bad_layout():
    source = build_report_source(
        [{"row_data": {"Voucher ID": "V1", "Date": "2026-01-05", "Debit": "bad"}}],
        [],
    )

    assert not source.ready
    codes = {item["code"] for item in source.blocking_diagnostics()}
    assert "missing_journal_columns" in codes
    assert source.journal.empty


def test_build_report_source_warns_when_journal_account_is_not_in_coa():
    source = build_report_source(_journal_rows(), [{"Account Code": "B1", "Cat": "Bank"}])

    assert source.ready
    warning = next(item for item in source.diagnostics if item["code"] == "journal_accounts_missing_from_coa")
    assert warning["count"] == 1


def test_load_report_source_uses_selected_upload_and_assignment_fallback():
    calls = []

    def query(sql, params=None):
        calls.append((sql, params))
        if "FROM client_data" in sql:
            return _journal_rows()
        if "FROM trial_balance_accounts" in sql:
            return [{
                "account_code": "B1",
                "account_name": "Bank A",
                "source_account_type": "Bank",
                "category_name": None,
            }]
        if "FROM account_category_assignments" in sql:
            return [{"account_code": "C1", "category_name": "Customers"}]
        raise AssertionError(f"Unexpected query: {sql}")

    source = load_ledger_report_source(
        7,
        journal_upload_id=12,
        period_start="2026-01-01",
        period_end="2026-01-31",
        query_fn=query,
    )

    assert source.ready
    assert source.metadata["client_id"] == 7
    assert source.metadata["journal_upload_id"] == 12
    assert source.coa.set_index("account_code").loc["C1", "category"] == "Customers"
    assert calls[0][1] == (7, 12)


def test_load_report_source_rejects_ambiguous_selector():
    try:
        load_ledger_report_source(7, journal_upload_id=12, journal_template_id=9)
    except ValueError as error:
        assert "not both" in str(error)
    else:
        raise AssertionError("Expected conflicting source selectors to be rejected")


def test_load_report_source_blocks_multiple_datasets_without_selection():
    rows = _journal_rows()
    rows[1] = dict(rows[1], template_id=10, upload_id=13)

    def query(sql, params=None):
        if "FROM client_data" in sql:
            return rows
        if "FROM trial_balance_accounts" in sql or "FROM account_category_assignments" in sql:
            return []
        raise AssertionError(f"Unexpected query: {sql}")

    source = load_ledger_report_source(7, query_fn=query)

    assert not source.ready
    assert any(item["code"] == "ambiguous_journal_source" for item in source.blocking_diagnostics())
