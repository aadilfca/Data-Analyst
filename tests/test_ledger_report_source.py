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


def test_load_report_source_uses_selected_upload_and_report_category_assignment():
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
            }]
        if "FROM report_category_sets" in sql:
            return [{"id": 31}]
        if "FROM report_account_category_assignments" in sql:
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
    assert not any(
        "audit_rule_categories" in sql or "FROM account_category_assignments" in sql
        for sql, _ in calls
    )
    assert calls[0][1] == (7, 12)


def test_load_report_source_warns_when_ledger_summary_category_set_is_missing():
    def query(sql, params=None):
        if "FROM client_data" in sql:
            return _journal_rows()
        if "FROM trial_balance_accounts" in sql:
            return []
        if "FROM report_category_sets" in sql:
            return []
        raise AssertionError(f"Unexpected query: {sql}")

    source = load_ledger_report_source(7, journal_upload_id=12, query_fn=query)

    assert source.ready
    assert any(item["code"] == "no_ledger_summary_category_set" for item in source.diagnostics)


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
        if "FROM trial_balance_accounts" in sql or "FROM report_category_sets" in sql:
            return []
        raise AssertionError(f"Unexpected query: {sql}")

    source = load_ledger_report_source(7, query_fn=query)

    assert not source.ready
    assert any(item["code"] == "ambiguous_journal_source" for item in source.blocking_diagnostics())


def test_build_report_source_accepts_account_detail_as_account_code():
    rows = [{
        "row_data": {
            "Voucher ID": "V1",
            "Transaction Date": "2026-01-05",
            "Account Detail": "B1",
            "Account Name": "Bank A",
            "Debit Amount": 100,
            "Credit Amount": 0,
        }
    }]
    source = build_report_source(rows, [{"Account Code": "B1", "Cat": "Bank"}])

    assert source.ready
    assert source.journal.loc[0, "account_code"] == "B1"
    assert not any(item["code"] == "missing_journal_columns" for item in source.diagnostics)
    assert not any(item["code"] == "ambiguous_journal_source" for item in source.blocking_diagnostics())


def test_build_report_source_prefers_explicit_cashflow_column_mapping():
    rows = [{
        "row_data": {
            "Reference column": "V1",
            "Date column": "2026-01-05",
            "Code column": "B1",
            "Debit column": 100,
            "Credit column": 0,
        }
    }]
    mapping = {
        "voucher_id": "Reference column",
        "date": "Date column",
        "account_code": "Code column",
        "debit": "Debit column",
        "credit": "Credit column",
    }
    source = build_report_source(rows, [{"Account Code": "B1", "Cat": "Bank"}], column_mapping=mapping)

    assert source.ready
    assert source.journal.loc[0, "voucher_id"] == "V1"
    assert source.journal.loc[0, "account_code"] == "B1"
    assert source.journal.loc[0, "debit"] == 100


def test_load_cashflow_source_applies_saved_template_mapping():
    rows = [{
        "template_id": 1,
        "upload_id": 12,
        "row_data": {
            "Reference column": "V1",
            "Date column": "2026-01-05",
            "Code column": "B1",
            "Debit column": 100,
            "Credit column": 0,
        },
    }]
    mapping_rows = [
        {"role": "voucher_id", "column_name": "Reference column"},
        {"role": "date", "column_name": "Date column"},
        {"role": "account_code", "column_name": "Code column"},
        {"role": "debit", "column_name": "Debit column"},
        {"role": "credit", "column_name": "Credit column"},
    ]

    def query(sql, params=None):
        if "FROM client_data" in sql:
            return rows
        if "FROM account_control_cashflow_source_mappings" in sql:
            return mapping_rows
        if "FROM trial_balance_accounts" in sql:
            return [{"account_code": "B1", "account_name": "Bank A", "source_account_type": "Bank"}]
        if "FROM account_control_cashflow_category_assignments" in sql:
            return []
        raise AssertionError(f"Unexpected query: {sql}")

    source = load_ledger_report_source(
        7,
        journal_upload_id=12,
        period_start="2026-01-01",
        period_end="2026-01-31",
        query_fn=query,
        account_category_source="cashflow",
    )

    assert source.ready
    assert source.metadata["cashflow_mapping_ready"] is True
    assert source.journal.loc[0, "account_code"] == "B1"
