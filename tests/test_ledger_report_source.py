import pandas as pd

from services.ledger_report_source import (
    advance_cashflow_report_revision,
    build_report_source,
    canonical_cashflow_voucher_selection_cte,
    load_cashflow_prepared_classification_preview,
    load_cashflow_prepared_rollup_summary,
    load_ledger_report_source,
    select_canonical_cashflow_vouchers,
    select_cashflow_report_source_identity,
)


def test_cashflow_report_invalidation_keeps_one_compatibility_marker():
    writes = []

    def write(sql, params=None):
        writes.append((sql, params))
        return 17

    assert advance_cashflow_report_revision(
        7,
        reason="source_imported",
        changed_by="analyst",
        write_fn=write,
    ) == 17
    assert len(writes) == 1
    assert "revision = cashflow_report_revisions.revision + 1" not in writes[0][0]


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


def test_report_source_prefers_active_prepared_cashflow_revision():
    calls = []

    def query(sql, params=None):
        calls.append((sql, params))
        if "cashflow_preparation_state" in sql:
            return [{"active_preparation_revision": 17, "status": "ready"}]
        assert "FROM cashflow_prepared_lines" in sql
        return [
            {
                "voucher_id": "V1",
                "transaction_date": "2026-01-05",
                "account_code": "BANK",
                "account_name": "Bank",
                "debit": 100,
                "credit": 0,
                "treasury_type": "bank",
                "category_label_snapshot": "",
                "transaction_month": "2026-01",
                "transaction_fortnight": "2026-01-F1",
                "upload_id": 12,
                "source_client_data_id": 101,
            },
            {
                "voucher_id": "V1",
                "transaction_date": "2026-01-05",
                "account_code": "EXP",
                "account_name": "Expense",
                "debit": 0,
                "credit": 100,
                "treasury_type": "",
                "category_label_snapshot": "Operating",
                "transaction_month": "2026-01",
                "transaction_fortnight": "2026-01-F1",
                "upload_id": 12,
                "source_client_data_id": 102,
            },
        ]

    source = load_ledger_report_source(
        7,
        journal_template_id=9,
        period_start="2026-01-01",
        period_end="2026-01-31",
        account_category_source="cashflow",
        query_fn=query,
    )

    assert source.ready
    assert source.metadata["preparation_revision"] == 17
    assert source.metadata["cashflow_report_source_mode"] == "prepared_revision"
    assert source.journal["voucher_id"].tolist() == ["V1", "V1"]
    assert source.journal["transaction_fortnight"].tolist() == ["2026-01-F1", "2026-01-F1"]
    assert len(calls) == 2


def test_prepared_rollup_summary_reads_active_revision():
    calls = []

    def query(sql, params=None):
        calls.append((sql, params))
        if "cashflow_preparation_state" in sql:
            return [{"active_preparation_revision": 17, "status": "ready"}]
        if "COUNT(DISTINCT voucher_id)" in sql:
            return [{"period_key": "2026-01", "selected_voucher_count": 1}]
        assert "FROM cashflow_prepared_rollups" in sql
        return [
            {
                "period_key": "2026-01",
                "category_label_snapshot": "Customers",
                "account_code": "AR",
                "account_name": "Customers",
                "treasury_type": "",
                "debit_total": 0,
                "credit_total": 100,
                "net_total": -100,
                "line_count": 1,
            },
            {
                "period_key": "2026-01",
                "category_label_snapshot": "Bank",
                "account_code": "BANK",
                "account_name": "Bank",
                "treasury_type": "bank",
                "debit_total": 100,
                "credit_total": 0,
                "net_total": 100,
                "line_count": 1,
            },
        ]

    result = load_cashflow_prepared_rollup_summary(
        7,
        journal_template_id=9,
        period_start="2026-01-01",
        period_end="2026-01-31",
        group_by="month",
        receipt_presentation="net",
        query_fn=query,
    )

    assert result["metadata"]["cashflow_report_source_mode"] == "prepared_rollups"
    assert result["metadata"]["preparation_revision"] == 17
    assert result["summary"]["category_totals"].loc["Customers", "2026-01"] == 100.0
    assert result["summary"]["source_line_count"] == 2
    assert result["summary"]["selected_voucher_count"] == 1
    assert len(calls) == 3

def test_canonical_cashflow_voucher_selector_uses_only_current_self_linked_occurrences():
    calls = []

    def query(sql, params=None):
        calls.append((sql, params))
        assert "canonical_occurrence_id = occurrence.id" in sql
        assert "decision_status IN ('current', 'current_revised')" in sql
        assert "latest_template_uploads" not in sql
        return [
            {"canonical_occurrence_id": 41, "upload_id": 12},
            {"canonical_occurrence_id": 42, "upload_id": 13},
            {"canonical_occurrence_id": 43, "upload_id": 13},
        ]

    selection = select_canonical_cashflow_vouchers(7, 9, query)

    assert selection.ready
    assert selection.occurrence_count == 3
    assert selection.upload_ids == (12, 13)
    assert selection.source_key == "canonical-v1"
    assert calls[0][1] == (7, 9)


def test_canonical_cashflow_voucher_selector_requires_a_populated_register():
    selection = select_canonical_cashflow_vouchers(7, 9, lambda _sql, _params: [])

    assert not selection.ready
    assert selection.occurrence_count == 0
    assert selection.upload_ids == ()
    assert "selected_canonical_vouchers" in canonical_cashflow_voucher_selection_cte()


def test_source_identity_prefers_populated_canonical_register():
    calls = []

    def query(sql, params=None):
        calls.append((sql, params))
        assert "selected_canonical_vouchers" in sql
        assert "selected_blank_vouchers" in sql
        return [
            {"canonical_occurrence_id": 41, "upload_id": 12, "voucher_identity": "V1", "duplicate_uploads_suppressed": 3},
            {"canonical_occurrence_id": None, "upload_id": 13, "voucher_identity": "", "duplicate_uploads_suppressed": 3},
        ]

    identity = select_cashflow_report_source_identity(7, 9, query)

    assert identity.ready
    assert identity.selection_mode == "canonical_register"
    assert identity.upload_ids == (12, 13)
    assert identity.duplicate_uploads_suppressed == 3
    assert identity.source_key.startswith("canonical-v1:")
    assert len(calls) == 1

def test_fresh_start_source_identity_uses_latest_voucher_population_and_blank_rows():
    calls = []

    def query(sql, params=None):
        calls.append((sql, params))
        if "SELECT canonical_occurrence_id, upload_id" in sql:
            return []
        if "FROM cashflow_preparation_state" in sql:
            return []
        assert "PARTITION BY voucher_identity" in sql
        assert "selected_blank_vouchers" in sql
        assert "file_name" not in sql
        return [
            {"upload_id": 22, "voucher_identity": "BPV-1", "duplicate_uploads_suppressed": 2},
            {"upload_id": 24, "voucher_identity": "", "duplicate_uploads_suppressed": 2},
        ]

    identity = select_cashflow_report_source_identity(7, 9, query)

    assert identity.ready
    assert identity.selection_mode == "latest_voucher"
    assert identity.upload_ids == (22, 24)
    assert identity.duplicate_uploads_suppressed == 2
    assert identity.source_key.startswith("latest-voucher-v1:")
    assert calls[-1][1] == (7, 9, 7, 7, 9, 7, 9)

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
        if "FROM uploaded_files" in sql:
            return [
                {"upload_id": 12, "file_name": "month.xlsx", "upload_date": "2026-01-01"},
                {"upload_id": 13, "file_name": "month.xlsx", "upload_date": "2026-01-02"},
            ]
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
    assert calls[0][1] == (7, 12, "2026-01-01", "2026-01-31")


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


def test_load_report_source_applies_template_upload_deduplication_query():
    rows = _journal_rows()
    rows[1] = dict(rows[1], upload_id=13)
    calls = []

    def query(sql, params=None):
        calls.append((sql, params))
        if "FROM uploaded_files" in sql:
            return [
                {"upload_id": 12, "file_name": "month.xlsx", "upload_date": "2026-01-01"},
                {"upload_id": 13, "file_name": "month.xlsx", "upload_date": "2026-01-02"},
            ]
        if "FROM client_data" in sql:
            return rows
        if "FROM trial_balance_accounts" in sql or "FROM report_category_sets" in sql:
            return []
        raise AssertionError(f"Unexpected query: {sql}")

    source = load_ledger_report_source(7, journal_template_id=9, query_fn=query)

    assert source.ready
    assert len(source.journal) == 2
    assert source.metadata["source_row_count"] == 2
    assert source.metadata["source_upload_ids"] == [12, 13]
    assert calls[0][1] == (7, 7, 9)

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


def test_cashflow_template_query_filters_period_and_treasury_vouchers():
    rows = _journal_rows()
    mapping_rows = [
        {"role": "voucher_id", "column_name": "Voucher ID"},
        {"role": "date", "column_name": "Date"},
        {"role": "account_code", "column_name": "Account Code"},
        {"role": "debit", "column_name": "Debit"},
        {"role": "credit", "column_name": "Credit"},
    ]
    calls = []

    def query(sql, params=None):
        calls.append((sql, params))
        if "SELECT canonical_occurrence_id, upload_id" in sql:
            return []
        if "FROM cashflow_preparation_state" in sql:
            return []
        if "FROM selected_report_vouchers" in sql:
            return []
        if "FROM account_control_cashflow_source_mappings" in sql:
            return mapping_rows
        if "FROM uploaded_files" in sql:
            return [
                {"upload_id": 12, "file_name": "month.xlsx", "upload_date": "2026-01-01"},
                {"upload_id": 13, "file_name": "month.xlsx", "upload_date": "2026-01-02"},
            ]
        if "FROM client_data" in sql:
            return rows
        if "FROM trial_balance_accounts" in sql or "FROM account_control_cashflow_category_assignments" in sql:
            return []
        raise AssertionError(f"Unexpected query: {sql}")

    source = load_ledger_report_source(
        7,
        journal_template_id=9,
        period_start="2026-01-01",
        period_end="2026-01-31",
        query_fn=query,
        account_category_source="cashflow",
        treasury_account_codes=["B1"],
    )

    source_query, source_params = next((sql, params) for sql, params in calls if "SELECT cd.id AS client_data_id" in sql)
    assert source.ready
    assert source.metadata["date_prefilter_applied"] is True
    assert source.metadata["duplicate_upload_suppression"] is True
    assert source.metadata["treasury_prefilter_applied"] is False
    assert source.metadata["treasury_filter_stage"] == "cashflow_summary"
    assert "cd.extract_date >= %s" in source_query
    assert "cd.extract_date <= %s" in source_query
    assert "cd.upload_id = ANY(%s)" in source_query
    assert any("FROM uploaded_files" in sql for sql, _ in calls)
    assert "selected_vouchers" not in source_query
    assert "ANY(%s)" in source_query
    assert source_params == (7, 9, "2026-01-01", "2026-01-31", [12])

def test_build_report_source_forward_fills_dates_and_parses_excel_dates_and_amounts():
    excel_serial = int((pd.Timestamp("2026-01-05") - pd.Timestamp("1899-12-30")).days)
    source = build_report_source([
        {"row_data": {"Voucher ID": "V1", "Date": excel_serial, "Account Code": "B1", "Debit": "1,000.00", "Credit": ""}},
        {"row_data": {"Voucher ID": "", "Date": "", "Account Code": "C1", "Debit": "", "Credit": "(100.00)"}},
    ])

    assert source.ready
    assert len(source.journal) == 2
    assert source.journal["date"].tolist() == [pd.Timestamp("2026-01-05"), pd.Timestamp("2026-01-05")]
    assert source.journal.loc[1, "credit"] == -100.0
    assert source.metadata["rejected_row_count"] == 0


def test_build_report_source_quarantines_layout_and_invalid_rows_as_warnings():
    source = build_report_source([
        {"row_data": {"Voucher ID": "V1", "Date": "05-01-2026", "Account Code": "B1", "Debit": 100, "Credit": 0}},
        {"row_data": {"Voucher ID": "Voucher ID", "Date": "Date", "Account Code": "Account Code", "Debit": "Debit", "Credit": "Credit"}},
        {"row_data": {"Voucher ID": "", "Date": "", "Account Code": "", "Account Name": "Total", "Debit": "", "Credit": ""}},
        {"row_data": {"Voucher ID": "V2", "Date": "not-a-date", "Account Code": "E1", "Debit": 25, "Credit": 0}},
        {"row_data": {"Voucher ID": "V3", "Date": "05/01/2026", "Account Code": "E2", "Debit": "bad", "Credit": 0}},
    ])

    assert source.ready
    assert len(source.journal) == 1
    assert source.journal.loc[0, "date"] == pd.Timestamp("2026-01-05")
    assert source.metadata["layout_row_count"] == 2
    assert source.metadata["rejected_row_count"] == 4
    assert any(item["code"] == "journal_layout_rows_removed" for item in source.diagnostics)
    assert any(item["code"] == "rejected_journal_rows" and item["severity"] == "warning" for item in source.diagnostics)
    assert not source.blocking_diagnostics()


def test_build_report_source_normalizes_date_valued_account_code():
    source = build_report_source([
        {"row_data": {"Voucher ID": "V1", "Date": "2026-01-05", "Account Code": pd.Timestamp("2026-01-05"), "Debit": 100, "Credit": 0}},
    ])

    assert source.ready
    assert source.journal.loc[0, "account_code"] == "01-05-2026"


def test_prepared_classification_preview_reads_active_prepared_lines():
    calls = []

    def query(sql, params=None):
        calls.append((sql, params))
        if "cashflow_preparation_state" in sql:
            return [{"active_preparation_revision": 17, "status": "ready"}]
        if "SELECT COUNT(*) AS total" in sql:
            return [{"total": 1}]
        assert "FROM cashflow_prepared_lines p" in sql
        return [{
            "id": 101,
            "row_data": {"Voucher ID": "V1", "Account": "BANK"},
            "extract_amount": 100,
            "extract_date": "2026-01-05",
            "data_quality_flag": None,
            "rate_source": None,
            "voucher_id": "V1",
            "account_code": "BANK",
            "account_name": "Bank",
            "transaction_date": "2026-01-05",
            "debit": 100,
            "credit": 0,
            "treasury_type": "bank",
            "classification_status": "bank",
            "category_label_snapshot": "Bank receipts",
            "transaction_month": "2026-01",
            "transaction_fortnight": "2026-01-F1",
        }]

    result = load_cashflow_prepared_classification_preview(
        5,
        9,
        "2026-01-01",
        "2026-01-31",
        treasury_view="combined",
        query_fn=query,
    )

    assert result["source_mode"] == "prepared_lines"
    assert result["preparation_revision"] == 17
    assert result["rows"][0]["id"] == 101
    assert result["rows"][0]["classification"] == {
        "type": "Bank",
        "account_code": "BANK",
        "account_name": "Bank",
        "assigned_category": "Bank receipts",
        "classification_status": "bank",
        "voucher_id": "V1",
        "transaction_date": "2026-01-05",
        "debit": 100,
        "credit": 0,
        "configuration_version": None,
        "transaction_month": "2026-01",
        "transaction_fortnight": "2026-01-F1",
    }
    assert any("FROM cashflow_prepared_lines p" in sql for sql, _ in calls)
def test_prepared_rollup_summary_projects_bank_view_from_prepared_lines():
    def query(sql, params=None):
        if "cashflow_preparation_state" in sql:
            return [{"active_preparation_revision": 17, "status": "ready"}]
        if "COUNT(DISTINCT voucher_id)" in sql:
            return [{"period_key": "2026-01", "selected_voucher_count": 1}]
        if "FROM cashflow_prepared_rollups" in sql:
            return [
                {
                    "period_key": "2026-01",
                    "category_label_snapshot": "Operating",
                    "account_code": "EXP",
                    "account_name": "Expense",
                    "treasury_type": "",
                    "debit_total": 0,
                    "credit_total": 100,
                    "net_total": -100,
                    "line_count": 1,
                },
                {
                    "period_key": "2026-01",
                    "category_label_snapshot": "Bank",
                    "account_code": "BANK",
                    "account_name": "Bank",
                    "treasury_type": "bank",
                    "debit_total": 100,
                    "credit_total": 0,
                    "net_total": 100,
                    "line_count": 1,
                },
            ]
        assert "WITH gateway AS" in sql
        assert "cashflow_prepared_lines" in sql
        return [{
            "category": "Operating",
            "period_key": "2026-01",
            "amount": -100,
            "bank_movement": 100,
            "cash_movement": 0,
        }]

    result = load_cashflow_prepared_rollup_summary(
        7,
        journal_template_id=9,
        period_start="2026-01-01",
        period_end="2026-01-31",
        group_by="month",
        receipt_presentation="net",
        treasury_view="bank",
        query_fn=query,
    )

    assert result["metadata"]["treasury_filter_stage"] == "prepared_rollups_bank"
    assert result["summary"]["category_totals"].loc["Operating", "2026-01"] == 100.0
    treasury = result["summary"]["treasury_category_totals"]
    row = treasury.loc[treasury["category"].eq("Operating")].iloc[0]
    assert row["bank_amount"] == 100.0
    assert row["cash_amount"] == 0.0
    assert result["summary"]["treasury_reconciliation"]["status"] == "Matched"
