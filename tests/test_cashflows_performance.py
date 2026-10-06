"""Phase 6 coverage for SQL-first Cashflows source preparation."""

import inspect

import database
from services.ledger_report_source import (
    CASHFLOW_DRILLDOWN_DETAILS_VERSION,
    _cashflow_daily_rollup,
    _cashflow_drilldown_integrity,
    _cashflow_summary_from_payload,
    _cashflow_summary_payload,
    load_cashflow_prepared_summary,
    load_cashflow_sql_source,
    load_cashflow_summary_read_model,
)


def test_cashflow_sqlite_and_postgres_schema_contracts_stay_aligned():
    sqlite_schema = inspect.getsource(database.init_db)
    postgres_schema = inspect.getsource(database.init_db_postgres)
    required_tables = {
        "cash_bank_summary_read_models": (
            "client_id",
            "template_id",
            "source_upload_ids_key",
            "configuration_version",
            "report_revision",
            "period_start",
            "period_end",
            "group_by",
            "receipt_presentation",
            "summary_payload",
        ),
        "cashflow_report_revisions": (
            "client_id",
            "revision",
            "changed_at",
            "changed_by",
            "change_reason",
        ),
    }
    required_indexes = (
        "idx_cash_bank_summary_read_models_client",
        "idx_cash_bank_classifications_client_date",
        "idx_cash_bank_classifications_client_voucher",
        "idx_cash_bank_classifications_client_account_date",
        "idx_cash_bank_classifications_source_version",
        "idx_cash_bank_classifications_client_period",
    )

    for table, columns in required_tables.items():
        for schema in (sqlite_schema, postgres_schema):
            assert f"CREATE TABLE IF NOT EXISTS {table}" in schema
            for column in columns:
                assert column in schema
    for index_name in required_indexes:
        assert index_name in sqlite_schema
        assert index_name in postgres_schema


def test_cashflow_sql_source_filters_dates_joins_vouchers_and_aggregates_lines():
    calls = []

    def query(sql, params=None):
        calls.append((sql, params))
        if "FROM uploaded_files" in sql:
            return [
                {"upload_id": 12, "file_name": "month.xlsx", "upload_date": "2026-01-02"},
                {"upload_id": 11, "file_name": "month.xlsx", "upload_date": "2026-01-01"},
            ]
        return [
            {
                "voucher_id": "V1",
                "date": "2026-01-05",
                "account_code": "B1",
                "account_name": "Bank",
                "voucher_detail": "Receipt",
                "debit": 100,
                "credit": 0,
                "treasury_type": "bank",
                "category": "",
                "source_line_count": 2,
                "classification_status": "cash",
            },
            {
                "voucher_id": "V1",
                "date": "2026-01-05",
                "account_code": "EXP",
                "account_name": "Expense",
                "voucher_detail": "Receipt",
                "debit": 0,
                "credit": 100,
                "treasury_type": None,
                "category": "Repairs",
                "source_line_count": 1,
                "classification_status": "cash",
            },
        ]

    source = load_cashflow_sql_source(
        7,
        journal_template_id=9,
        period_start="2026-01-01",
        period_end="2026-01-31",
        query_fn=query,
    )

    assert source is not None
    assert source.ready
    assert source.metadata["sql_aggregated"] is True
    assert source.metadata["raw_json_parsing"] is False
    assert source.metadata["duplicate_uploads_suppressed"] == 1
    assert source.metadata["source_row_count"] == 3
    assert source.journal.loc[0, "account_code"] == "B1"
    assert source.coa.set_index("account_code").loc["EXP", "category"] == "Repairs"

    assert len(calls) == 2
    source_sql, source_params = calls[1]
    assert "c.is_current = TRUE" in source_sql
    assert "c.transaction_date >= %s" in source_sql
    assert "c.transaction_date <= %s" in source_sql
    assert "selected_vouchers" in source_sql
    assert "GROUP BY l.upload_id, l.voucher_id, l.date, l.account_code" in source_sql
    assert "c.transaction_date AS date" in source_sql
    assert "c.account_name" in source_sql
    assert "FROM client_data cd" in source_sql
    assert source_params == (7, [12], "2026-01-01", "2026-01-31")


def test_cashflow_sql_source_scopes_duplicate_vouchers_to_their_upload():
    calls = []

    def query(sql, params=None):
        calls.append((sql, params))
        if "FROM uploaded_files" in sql:
            return [
                {"upload_id": 12, "file_name": "january.xlsx", "upload_date": "2026-02-01"},
                {"upload_id": 11, "file_name": "february.xlsx", "upload_date": "2026-02-01"},
            ]
        assert params == (7, [11, 12], "2026-01-01", "2026-02-28")
        assert "SELECT DISTINCT upload_id, voucher_id" in sql
        assert "v.upload_id = l.upload_id" in sql
        assert "v.voucher_id = l.voucher_id" in sql
        return [
            {
                "voucher_id": "V1",
                "date": "2026-01-05",
                "account_code": "B1",
                "account_name": "Bank January",
                "voucher_detail": "January receipt",
                "debit": 100,
                "credit": 0,
                "treasury_type": "bank",
                "category": "",
                "source_line_count": 1,
                "classification_status": "cash",
            },
            {
                "voucher_id": "V1",
                "date": "2026-02-05",
                "account_code": "EXP",
                "account_name": "Expense February",
                "voucher_detail": "February payment",
                "debit": 0,
                "credit": 100,
                "treasury_type": None,
                "category": "Repairs",
                "source_line_count": 1,
                "classification_status": "cash",
            },
        ]

    source = load_cashflow_sql_source(
        7,
        journal_template_id=9,
        period_start="2026-01-01",
        period_end="2026-02-28",
        query_fn=query,
    )

    assert source is not None
    assert source.journal["voucher_id"].tolist() == ["V1", "V1"]
    assert source.journal["date"].dt.strftime("%Y-%m-%d").tolist() == ["2026-01-05", "2026-02-05"]
    assert source.metadata["source_upload_ids"] == [11, 12]
    assert len(calls) == 2


def test_cashflow_sql_source_explicit_upload_uses_upload_identity_filter():
    calls = []

    def query(sql, params=None):
        calls.append((sql, params))
        assert "FROM uploaded_files" not in sql
        assert "c.upload_id = ANY(%s)" in sql
        assert "c.template_id = %s" not in sql
        assert params == (7, [12], "2026-01-01", "2026-01-31")
        return [{
            "voucher_id": "V1",
            "date": "2026-01-05",
            "account_code": "B1",
            "account_name": "Bank",
            "voucher_detail": "Receipt",
            "debit": 100,
            "credit": 0,
            "treasury_type": "bank",
            "category": "",
            "source_line_count": 1,
            "classification_status": "cash",
        }]

    source = load_cashflow_sql_source(
        7,
        journal_upload_id=12,
        period_start="2026-01-01",
        period_end="2026-01-31",
        query_fn=query,
    )

    assert source is not None
    assert source.metadata["source_upload_ids"] == [12]
    assert len(calls) == 1
def test_cashflow_sql_source_preserves_each_transaction_date_for_bucketing():
    def query(sql, params=None):
        if "FROM uploaded_files" in sql:
            return [{"upload_id": 12, "file_name": "period.xlsx", "upload_date": "2026-02-01"}]
        return [
            {
                "voucher_id": "V1",
                "date": "2026-01-05",
                "account_code": "B1",
                "account_name": "Bank",
                "voucher_detail": "January receipt",
                "debit": 100,
                "credit": 0,
                "treasury_type": "bank",
                "category": "",
                "source_line_count": 2,
                "classification_status": "cash",
            },
            {
                "voucher_id": "V2",
                "date": "2026-02-05",
                "account_code": "B1",
                "account_name": "Bank",
                "voucher_detail": "February receipt",
                "debit": 75,
                "credit": 0,
                "treasury_type": "bank",
                "category": "",
                "source_line_count": 2,
                "classification_status": "cash",
            },
        ]

    source = load_cashflow_sql_source(
        7,
        journal_template_id=9,
        period_start="2026-01-01",
        period_end="2026-02-28",
        query_fn=query,
    )

    assert source.journal["date"].dt.strftime("%Y-%m-%d").tolist() == ["2026-01-05", "2026-02-05"]
def test_cashflow_read_models_require_current_report_revision_when_requested():
    calls = []

    def query(sql, params=None):
        if "FROM uploaded_files" in sql:
            return [{"upload_id": 12, "file_name": "period.xlsx", "upload_date": "2026-02-01"}]
        if "MIN(c.transaction_date)" in sql:
            return [{"period_start": "2026-01-01", "period_end": "2026-02-28"}]
        if "FROM cash_bank_summary_read_models" in sql:
            calls.append((sql, params))
            assert "AND report_revision = %s" in sql
            assert params[-1] == 12
            return []
        raise AssertionError(f"Unexpected read-model query: {sql}")

    assert load_cashflow_prepared_summary(
        7,
        journal_template_id=9,
        period_start="2026-01-01",
        period_end="2026-01-31",
        group_by="month",
        receipt_presentation="net",
        report_revision=12,
        query_fn=query,
    ) is None
    assert load_cashflow_summary_read_model(
        7,
        journal_template_id=9,
        period_start="2026-01-01",
        period_end="2026-01-31",
        group_by="month",
        receipt_presentation="net",
        report_revision=12,
        query_fn=query,
    ) is None
    assert len(calls) == 2


def test_cashflow_daily_rollup_prepares_ledger_details_for_target_periods():
    payload = {
        "periods": [
            {"key": "2026-01-05", "start": "2026-01-05", "end": "2026-01-05", "label": "05 Jan"},
            {"key": "2026-01-06", "start": "2026-01-06", "end": "2026-01-06", "label": "06 Jan"},
            {"key": "2026-02-05", "start": "2026-02-05", "end": "2026-02-05", "label": "05 Feb"},
        ],
        "categories": [
            {"category": "Operating", "values": {"2026-01-05": 60, "2026-01-06": 40, "2026-02-05": 25}},
        ],
        "cashflow_ledger_rollups": [
            {"category": "Operating", "period_key": "2026-01-05", "period_label": "05 Jan", "account_code": "EXP", "account_name": "Operating", "treasury_type": "Bank", "amount": 60, "line_count": 1},
            {"category": "Operating", "period_key": "2026-01-06", "period_label": "06 Jan", "account_code": "EXP", "account_name": "Operating", "treasury_type": "Bank", "amount": 40, "line_count": 2},
            {"category": "Operating", "period_key": "2026-02-05", "period_label": "05 Feb", "account_code": "EXP", "account_name": "Operating", "treasury_type": "Cash", "amount": 25, "line_count": 1},
        ],
        "drilldown_details_prepared": True,
        "drilldown_details_version": CASHFLOW_DRILLDOWN_DETAILS_VERSION,
    }

    summary = _cashflow_daily_rollup(payload, "2026-01-01", "2026-02-28", "month")
    rollups = summary["cashflow_ledger_rollups"].set_index(["category", "period_key", "account_code"])

    assert summary["cashflow_drilldown_prepared"] is True
    assert rollups.loc[("Operating", "2026-01", "EXP"), "amount"] == 100
    assert rollups.loc[("Operating", "2026-01", "EXP"), "line_count"] == 3
    assert rollups.loc[("Operating", "2026-02", "EXP"), "amount"] == 25
    assert all(row["status"] == "Matched" for row in summary["cashflow_drilldown_controls"]["category_controls"])
    valid, reason = _cashflow_drilldown_integrity(summary)
    assert valid is True and reason is None

    payload_round_trip = _cashflow_summary_payload(summary)
    assert payload_round_trip["drilldown_details_version"] == CASHFLOW_DRILLDOWN_DETAILS_VERSION
    restored = _cashflow_summary_from_payload(payload_round_trip, "month")
    assert restored["cashflow_drilldown_prepared"] is True
    assert len(restored["cashflow_ledger_rollups"]) == 2


def test_cashflow_daily_rollup_preserves_treasury_totals_for_larger_periods():
    payload = {
        "periods": [
            {"key": "2026-01-05", "start": "2026-01-05", "end": "2026-01-05", "label": "05 Jan"},
            {"key": "2026-02-05", "start": "2026-02-05", "end": "2026-02-05", "label": "05 Feb"},
        ],
        "categories": [
            {"category": "Operating", "values": {"2026-01-05": 100, "2026-02-05": 25}},
        ],
        "treasury_category_totals": [
            {"category": "Operating", "period_key": "2026-01-05", "bank_amount": 60, "cash_amount": 40, "mixed_amount": 0, "total_amount": 100},
            {"category": "Operating", "period_key": "2026-02-05", "bank_amount": 25, "cash_amount": 0, "mixed_amount": 0, "total_amount": 25},
        ],
        "treasury_totals_version": 1,
        "period_metrics": {
            "2026-01-05": {"source_line_count": 1, "selected_voucher_count": 1, "internal_transfer_count": 0, "category_review_count": 0},
            "2026-02-05": {"source_line_count": 1, "selected_voucher_count": 1, "internal_transfer_count": 0, "category_review_count": 0},
        },
    }

    summary = _cashflow_daily_rollup(payload, "2026-01-01", "2026-02-28", "month")
    split = summary["treasury_category_totals"].set_index(["category", "period_key"])

    assert split.loc[("Operating", "2026-01"), "bank_amount"] == 60
    assert split.loc[("Operating", "2026-01"), "cash_amount"] == 40
    assert split.loc[("Operating", "2026-02"), "total_amount"] == 25
    assert summary["treasury_reconciliation"]["status"] == "Matched"
