"""Phase 6 coverage for SQL-first Cashflows source preparation."""

import inspect
from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd
import pytest

import database
from routers import reconciliation
from services.ledger_group_summary import build_cashflow_summary
from services.ledger_report_source import (
    CASHFLOW_DRILLDOWN_DETAILS_VERSION,
    CASHFLOW_DRILLDOWN_PROJECTION_VERSION,
    _cashflow_daily_rollup,
    _cashflow_drilldown_integrity,
    _cashflow_summary_from_payload,
    _cashflow_summary_payload,
    load_cashflow_prepared_drilldown,
    load_cashflow_prepared_summary,
    load_cashflow_sql_source,
    load_cashflow_client_data_source,
    load_ledger_report_source,
    load_cashflow_summary_read_model,
    store_cashflow_summary_read_model,
)


def test_database_coverage_uses_the_existing_source_round_trip_and_snapshot():
    calls = []

    def query(sql, params=None):
        calls.append((sql, params))
        return [{"database_row_count": 23, "database_voucher_count": 7,
                 "database_totals_only": True}]

    source = load_cashflow_client_data_source(7, journal_template_id=9,
        period_start="2026-01-01", period_end="2026-06-30", query_fn=query)
    assert source.metadata["database_totals"]["row_count"] == 23
    assert source.metadata["database_totals"]["voucher_count"] == 7
    assert len(calls) == 1
    sql, params = calls[0]
    assert "COUNT(*) AS database_row_count" in sql
    assert "COUNT(DISTINCT NULLIF(BTRIM(cd.derived_voucher_id), ''))" in sql
    assert "LEFT JOIN report_lines l ON TRUE" in sql
    assert params == (7, 9, "2026-01-01", "2026-06-30") * 2
    assert not any(word in sql.upper() for word in ("INSERT INTO", "UPDATE CLIENT_DATA", "CREATE TABLE"))


def test_explicit_all_accounts_preserves_the_exact_derived_source_query():
    calls = []
    def query(sql, params):
        calls.append((sql, params))
        return [{'database_row_count':23, 'database_voucher_count':7, 'database_totals_only':True}]
    options = dict(client_id=7, journal_template_id=9, period_start='2026-01-01', period_end='2026-06-30',
        account_category_source='cashflow', treasury_account_codes=['B','C'], query_fn=query)
    old = load_ledger_report_source(**options)
    explicit = load_ledger_report_source(**options, selected_treasury_account_codes=['C','B','B'])
    assert calls[0] == calls[1]
    pd.testing.assert_frame_equal(old.journal, explicit.journal)
    assert set(old.metadata) == set(explicit.metadata)
    assert 'source_inflow' not in calls[0][0]


def test_selected_account_source_uses_one_read_and_derived_financial_columns_only():
    calls = []
    def query(sql, params):
        calls.append((sql, params))
        return [{'database_row_count':23, 'database_voucher_count':7, 'database_totals_only':True}]
    load_cashflow_client_data_source(7, journal_template_id=9, period_start='2026-01-01',
        period_end='2026-06-30', selected_treasury_account_codes=[' B ','B'], query_fn=query)
    assert len(calls) == 1
    sql, params = calls[0]
    assert params == (7,9,'2026-01-01','2026-06-30')*2 + ('B',)
    assert "AND account_code IN (%s)" in sql
    assert 'GROUP BY l.voucher_id, l.date, l.account_code' in sql
    assert 'SUM(GREATEST(l.debit, 0) + GREATEST(-l.credit, 0)) AS source_inflow' in sql
    assert 'cd.derived_debit' in sql and 'cd.derived_credit' in sql
    assert not any(name in sql for name in ("row_data ->> 'Debit", "row_data ->> 'Credit", 'cash_bank_transaction_classifications', 'cash_bank_summary_read_models'))
    assert not any(word in sql.upper() for word in ('INSERT INTO', 'UPDATE CLIENT_DATA', 'CREATE TABLE'))


def test_cashflow_sqlite_and_postgres_schema_contracts_stay_aligned():
    sqlite_schema = inspect.getsource(database.init_db)
    postgres_schema = (
        inspect.getsource(database.init_db_postgres)
        + inspect.getsource(database._ensure_postgres_cash_bank_transaction_classifications)
        + inspect.getsource(database._ensure_postgres_cashflow_voucher_occurrences)
    )
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
        "cashflow_ledger_rollup_read_models": (
            "client_id",
            "template_id",
            "source_upload_ids_key",
            "configuration_version",
            "report_revision",
            "source_group_by",
            "period_start",
            "period_end",
            "period_key",
            "period_label",
            "category",
            "account_code",
            "account_name",
            "treasury_type",
            "amount",
            "line_count",
            "category_total",
        ),
        "cashflow_report_revisions": (
            "client_id",
            "revision",
            "changed_at",
            "changed_by",
            "change_reason",
        ),
        "cashflow_voucher_occurrences": (
            "client_id",
            "template_id",
            "upload_id",
            "voucher_id",
            "voucher_identity",
            "occurrence_key",
            "voucher_fingerprint",
            "fingerprint_version",
            "transaction_date",
            "source_line_count",
            "decision_status",
            "canonical_occurrence_id",
        ),
    }
    required_indexes = (
        "idx_cash_bank_summary_read_models_client",
        "idx_cashflow_ledger_rollup_read_models_lookup",
        "idx_cashflow_ledger_rollup_read_models_account",
        "idx_cash_bank_classifications_client_date",
        "idx_cash_bank_classifications_client_voucher",
        "idx_cash_bank_classifications_client_account_date",
        "idx_cash_bank_classifications_source_version",
        "idx_cash_bank_classifications_client_period",
        "idx_cashflow_voucher_occurrences_lookup",
        "idx_cashflow_voucher_occurrences_upload",
        "idx_cashflow_voucher_occurrences_canonical",
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
        if "SELECT canonical_occurrence_id, upload_id" in sql:
            return []
        if "FROM selected_report_vouchers" in sql:
            return [{"upload_id": 12, "voucher_identity": "V1", "duplicate_uploads_suppressed": 1}]
        if "SELECT upload_id, file_name, upload_date" in sql:
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
    assert source.metadata["cashflow_source_identity_seconds"] >= 0
    assert source.metadata["cashflow_classification_query_seconds"] >= 0
    assert source.metadata["cashflow_source_transform_seconds"] >= 0

    assert len(calls) == 3
    source_sql, source_params = calls[-1]
    assert "c.is_current = TRUE" in source_sql
    assert "c.transaction_date >= %s" in source_sql
    assert "c.transaction_date <= %s" in source_sql
    assert "selected_vouchers" in source_sql
    assert "GROUP BY l.upload_id, l.voucher_id, l.date, l.account_code" in source_sql
    assert "c.transaction_date AS date" in source_sql
    assert "c.account_name" in source_sql
    assert "FROM client_data cd" in source_sql
    assert source_params == (7, 9, 7, 7, 9, 7, 9, 7, "2026-01-01", "2026-01-31")


def test_cashflow_sql_source_scopes_duplicate_vouchers_to_their_upload():
    calls = []

    def query(sql, params=None):
        calls.append((sql, params))
        if "SELECT canonical_occurrence_id, upload_id" in sql:
            return []
        if "FROM selected_report_vouchers" in sql:
            return [{"upload_id": 12, "voucher_identity": "V1", "duplicate_uploads_suppressed": 1}]
        if "SELECT upload_id, file_name, upload_date" in sql:
            return [
                {"upload_id": 12, "file_name": "january.xlsx", "upload_date": "2026-02-01"},
                {"upload_id": 11, "file_name": "february.xlsx", "upload_date": "2026-02-01"},
            ]
        assert params == (7, 9, 7, 7, 9, 7, 9, 7, "2026-01-01", "2026-02-28")
        assert "selected_report_vouchers" in sql
        assert "v.upload_id = l.upload_id" in sql
        assert "v.voucher_id = l.voucher_id" in sql
        return [{
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
        }]

    source = load_cashflow_sql_source(
        7,
        journal_template_id=9,
        period_start="2026-01-01",
        period_end="2026-02-28",
        query_fn=query,
    )

    assert source is not None
    assert source.journal["voucher_id"].tolist() == ["V1"]
    assert source.journal["date"].dt.strftime("%Y-%m-%d").tolist() == ["2026-02-05"]
    assert source.metadata["source_upload_ids"] == [12]
    assert source.metadata["cashflow_report_source_mode"] == "latest_voucher"
    assert source.metadata["duplicate_uploads_suppressed"] == 1
    assert len(calls) == 3


def test_cashflow_sql_source_explicit_upload_uses_upload_identity_filter():
    calls = []

    def query(sql, params=None):
        calls.append((sql, params))
        assert "FROM uploaded_files" not in sql
        assert "c.upload_id = ANY(%s)" in sql
        assert "c.template_id = %s" not in sql
        assert params == ([12], 7, "2026-01-01", "2026-01-31")
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
        if "SELECT canonical_occurrence_id, upload_id" in sql:
            return []
        if "FROM selected_report_vouchers" in sql:
            return [
                {"upload_id": 12, "voucher_identity": "V1", "duplicate_uploads_suppressed": 0},
                {"upload_id": 12, "voucher_identity": "V2", "duplicate_uploads_suppressed": 0},
            ]
        if "SELECT upload_id, file_name, upload_date" in sql:
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
        if "SELECT canonical_occurrence_id, upload_id" in sql:
            return []
        if "FROM selected_report_vouchers" in sql:
            return [{"upload_id": 12, "voucher_identity": "V1", "duplicate_uploads_suppressed": 0}]
        if "SELECT upload_id, file_name, upload_date" in sql:
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




def test_latest_voucher_source_key_scopes_read_model_lookup_without_filename_selection():
    calls = []

    def query(sql, params=None):
        if "SELECT canonical_occurrence_id, upload_id" in sql:
            return []
        if "FROM selected_report_vouchers" in sql:
            return [{"upload_id": 12, "voucher_identity": "V1", "duplicate_uploads_suppressed": 0}]
        if "FROM cash_bank_summary_read_models" in sql:
            calls.append((sql, params))
            assert params[2].startswith("latest-voucher-v1:")
            return []
        raise AssertionError(f"Unexpected source query: {sql}")

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
    assert len(calls) == 1

def test_cashflow_snapshot_persists_targeted_projection_rows_when_requested():
    writes = []
    summary = {
        "periods": [{"key": "2026-01-05", "start": "2026-01-05", "end": "2026-01-05", "label": "05 Jan"}],
        "months": ["2026-01"],
        "category_totals": pd.DataFrame({"2026-01-05": [100.0]}, index=["Operating"]),
        "cashflow_ledger_rollups": pd.DataFrame([
            {"category": "Operating", "period_key": "2026-01-05", "period_label": "05 Jan", "account_code": "EXP", "account_name": "Utilities", "treasury_type": "Bank", "amount": 100.0, "line_count": 2},
        ]),
        "cashflow_drilldown_prepared": True,
        "cashflow_drilldown_controls": {"cash_bank_split": {"status": "Matched"}},
    }

    def write(sql, params=None):
        writes.append((sql, params))

    store_cashflow_summary_read_model(
        7,
        journal_template_id=9,
        source_upload_ids=[21],
        configuration_version=3,
        report_revision=4,
        source_key="canonical-v1",
        period_start="2026-01-05",
        period_end="2026-01-05",
        group_by="day",
        summary=summary,
        write_fn=write,
        write_drilldown_projection=True,
    )

    assert len(writes) == 1
    assert "cash_bank_summary_read_models" in writes[0][0]
    assert "cashflow_ledger_rollup_read_models" not in writes[0][0]
    assert writes[0][1][2] == "canonical-v1"


def test_cashflow_snapshot_deduplicates_projection_conflict_keys_before_bulk_upsert():
    writes = []
    summary = {
        "periods": [{"key": "2026-01-05", "start": "2026-01-05", "end": "2026-01-05", "label": "05 Jan"}],
        "months": ["2026-01"],
        "category_totals": pd.DataFrame({"2026-01-05": [150.0]}, index=["Operating"]),
        "cashflow_ledger_rollups": pd.DataFrame([
            {"category": "Operating", "period_key": "2026-01-05", "period_label": "05 Jan", "account_code": "EXP", "account_name": "Utilities", "treasury_type": "Bank", "amount": 100.0, "line_count": 2},
            {"category": "Operating", "period_key": "2026-01-05", "period_label": "05 Jan", "account_code": "EXP", "account_name": "Utilities (duplicate source line)", "treasury_type": "Bank", "amount": 50.0, "line_count": 1},
        ]),
        "cashflow_drilldown_prepared": True,
    }

    def write(sql, params=None):
        writes.append((sql, params))

    store_cashflow_summary_read_model(
        7,
        journal_template_id=9,
        source_upload_ids=[21],
        configuration_version=3,
        report_revision=4,
        source_key="canonical-v1",
        period_start="2026-01-05",
        period_end="2026-01-05",
        group_by="day",
        summary=summary,
        write_fn=write,
        write_drilldown_projection=True,
    )

    assert len(writes) == 1
    assert "cashflow_ledger_rollup_read_models" not in writes[0][0]
    assert "cashflow_ledger_rollups" in writes[0][1][-1]


def test_cashflow_prepared_drilldown_loader_targets_one_category_and_reaggregates_periods():
    calls = []

    def query(sql, params=None):
        if "FROM cashflow_preparation_state" in sql:
            return [{"active_preparation_revision": 4, "status": "ready"}]
        if "SELECT canonical_occurrence_id, upload_id" in sql:
            return []
        if "FROM selected_report_vouchers" in sql:
            return [{"upload_id": 21, "voucher_identity": "V1", "duplicate_uploads_suppressed": 0}]
        if "SELECT upload_id, file_name, upload_date" in sql:
            return [{"upload_id": 21, "file_name": "period.xlsx", "upload_date": "2026-03-01"}]
        if "FROM cashflow_prepared_lines" in sql:
            calls.append((sql, params))
            return [
                {"period_start": "2026-01-05", "period_key": "2026-01-05", "period_label": "05 Jan", "category": "Operating", "account_code": "EXP1", "account_name": "Utilities", "treasury_type": "Bank", "amount": 60, "line_count": 2, "category_total": 100},
                {"period_start": "2026-01-05", "period_key": "2026-01-05", "period_label": "05 Jan", "category": "Operating", "account_code": "EXP2", "account_name": "Rent", "treasury_type": "Cash", "amount": 40, "line_count": 1, "category_total": 100},
                {"period_start": "2026-01-06", "period_key": "2026-01-06", "period_label": "06 Jan", "category": "Operating", "account_code": "EXP1", "account_name": "Utilities", "treasury_type": "Bank", "amount": 30, "line_count": 1, "category_total": 50},
                {"period_start": "2026-01-06", "period_key": "2026-01-06", "period_label": "06 Jan", "category": "Operating", "account_code": "EXP2", "account_name": "Rent", "treasury_type": "Cash", "amount": 20, "line_count": 1, "category_total": 50},
                {"period_start": "2026-02-05", "period_key": "2026-02-05", "period_label": "05 Feb", "category": "Operating", "account_code": "EXP1", "account_name": "Utilities", "treasury_type": "Bank", "amount": 25, "line_count": 1, "category_total": 25},
            ]
        raise AssertionError(f"Unexpected query: {sql}")

    result = load_cashflow_prepared_drilldown(
        7,
        journal_template_id=9,
        period_start="2026-01-01",
        period_end="2026-02-28",
        group_by="month",
        category="Operating",
        report_revision=4,
        query_fn=query,
    )

    assert result is not None
    assert result["metadata"]["targeted_prepared_lines"] is True
    assert result["metadata"]["summary_read_model"] == "prepared_lines"
    totals = {}
    for row in result["rows"]:
        totals[row["period_key"]] = totals.get(row["period_key"], 0) + row["amount"]
    assert totals == {"2026-01": 150.0, "2026-02": 25.0}
    assert result["controls"]["category_controls"][0]["status"] == "Matched"
    assert len(calls) == 1
    assert "category = %s" in calls[0][0]
    assert calls[0][1][-1] == "Operating"


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
    summary_only = _cashflow_daily_rollup(
        payload,
        "2026-01-01",
        "2026-02-28",
        "month",
        include_drilldown=False,
    )
    rollups = summary["cashflow_ledger_rollups"].set_index(["category", "period_key", "account_code"])

    assert summary_only["cashflow_drilldown_prepared"] is False
    assert summary_only["cashflow_ledger_rollups"].empty

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
    restored_summary_only = _cashflow_summary_from_payload(
        payload_round_trip,
        "month",
        include_drilldown=False,
    )
    assert restored["cashflow_drilldown_prepared"] is True
    assert len(restored["cashflow_ledger_rollups"]) == 2
    assert restored_summary_only["cashflow_drilldown_prepared"] is False
    assert restored_summary_only["cashflow_ledger_rollups"].empty


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


@pytest.mark.parametrize('selection',[None,['B1']])
def test_browser_warmup_reuses_one_canonical_read_without_persistent_copies(selection):
    """Warm-up changes grouping work, never the source read/write contract."""
    journal = pd.DataFrame([
        dict(voucher_id=f'V{i}', date='2026-01-05', account_code=code,
             account_name=code, debit=100 if code == 'B1' else 0,
             credit=0 if code == 'B1' else 100, voucher_detail='Synthetic receipt')
        for i in range(100) for code in ('B1', f'AR{i % 10}')
    ])
    coa = pd.DataFrame([dict(account_code=code, account_title=code, account_type='',
                            category='' if code == 'B1' else 'Receipts')
                        for code in journal['account_code'].unique()])
    configuration = dict(source_template_id=9, cash_account_codes=[], bank_account_codes=['B1','B2'],
                         match_internal_transfers=False, receipt_presentation='net')
    source = SimpleNamespace(journal=journal, coa=coa, diagnostics=(), ready=True,
                             metadata={'cashflow_report_source_key': 'client-data-v1:7:9'})
    with (
        patch.object(reconciliation, '_require_active_client'),
        patch.object(reconciliation, '_cashflow_configuration', return_value=configuration),
        patch.object(reconciliation, 'load_ledger_report_source', return_value=source) as loader,
        patch.object(reconciliation, 'build_cashflow_summary', wraps=build_cashflow_summary) as builder,
        patch.object(reconciliation, 'execute_write_pg', side_effect=AssertionError('No writes')),
        patch.object(reconciliation, 'store_cashflow_summary_read_model', side_effect=AssertionError('No copies')),
        patch.object(reconciliation, 'run_read_snapshot_pg', side_effect=lambda callback: callback(None)),
    ):
        result = reconciliation.preview_cashflows(7, '2026-01-01', '2026-01-31',
            group_by='month', treasury_view='combined', details='summary', warm_ledger_cache=True, session=None,
            selected_treasury_account_codes=selection)
    assert loader.call_count == builder.call_count == 1
    assert result['preview']['ledger_cache']['complete'] is True
    assert result['preview']['ledger_cache']['row_count'] == 10
    assert sum(row['amount'] for row in result['preview']['ledger_cache']['rows']) == 10000
    assert loader.call_args.kwargs.get('selected_treasury_account_codes')==selection
    if selection:
        assert result['preview']['ledger_cache']['context']['account_selection']['account_codes']==selection


def test_selected_report_api_retains_one_derived_source_query_without_financial_json_extraction():
    calls=[]
    def query(sql,params):
        calls.append((sql,params))
        return [{'database_row_count':23,'database_voucher_count':7,'database_totals_only':True}]
    configuration=dict(source_template_id=9,cash_account_codes=['C1'],bank_account_codes=['B1','B2'],
        match_internal_transfers=True,receipt_presentation='net')
    with patch.object(reconciliation,'_require_active_client'), \
            patch.object(reconciliation,'_cashflow_configuration',return_value=configuration), \
            patch.object(reconciliation,'execute_query_pg',side_effect=query), \
            patch.object(reconciliation,'run_read_snapshot_pg',side_effect=lambda callback: callback(None)), \
            patch.object(reconciliation,'execute_write_pg',side_effect=AssertionError('No writes')):
        result=reconciliation.preview_cashflows(7,'2026-01-01','2026-01-31',group_by='month',
            treasury_view='combined',details='summary',session=None,selected_treasury_account_codes=['B2','B1'])
    assert len(calls)==1
    sql,params=calls[0]
    assert 'cd.derived_debit' in sql and 'cd.derived_credit' in sql
    assert 'AND account_code IN (%s, %s)' in sql
    assert params[-2:]==('B1','B2')
    assert "row_data ->> 'Debit" not in sql and "row_data ->> 'Credit" not in sql
    assert result['account_selection']['account_codes']==['B1','B2']
