import sqlite3

import pandas as pd
import pytest
from test_reconciliation_balance_source import independent_year_boundary_case

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


def test_report_source_reads_active_client_data_derived_rows():
    calls = []

    def query(sql, params=None):
        calls.append((sql, params))
        assert "FROM client_data cd" in sql
        return [
            {
                "voucher_id": "V1",
                "date": "2026-01-05",
                "account_code": "BANK",
                "account_name": "Bank",
                "voucher_detail": "Receipt",
                "debit": 100,
                "credit": 0,
                "treasury_type": "bank",
                "category": "",
                "transaction_month": "2026-01",
                "transaction_fortnight": "2026-01-F1",
                "upload_id": 12,
                "source_line_count": 1,
            },
            {
                "voucher_id": "V1",
                "date": "2026-01-05",
                "account_code": "EXP",
                "account_name": "Expense",
                "voucher_detail": "Receipt",
                "debit": 0,
                "credit": 100,
                "treasury_type": "",
                "category": "Operating",
                "transaction_month": "2026-01",
                "transaction_fortnight": "2026-01-F1",
                "upload_id": 12,
                "source_line_count": 1,
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
    assert source.metadata["cashflow_report_source_mode"] == "client_data_derived"
    assert source.journal["voucher_id"].tolist() == ["V1", "V1"]
    assert source.journal["transaction_fortnight"].tolist() == ["2026-01-F1", "2026-01-F1"]
    assert len(calls) == 1


@pytest.fixture
def database_count_source():
    """Execute the source SELECT against disposable rows, not a SQL-shaped mock."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.create_function("BTRIM", 1, lambda value: value.strip() if value is not None else None)
    conn.executescript("""
        CREATE TABLE client_data (
            id INTEGER, client_id INTEGER, template_id INTEGER, upload_id INTEGER,
            is_deleted INTEGER, change_detection_status TEXT, derived_voucher_id TEXT,
            derived_transaction_date TEXT, derived_account_code TEXT, derived_account_name TEXT,
            derived_transaction_month TEXT, derived_transaction_fortnight TEXT, row_data TEXT,
            derived_debit REAL, derived_credit REAL, derived_treasury_type TEXT,
            derived_classification_status TEXT);
        CREATE TABLE account_control_cashflow_category_assignments
            (client_id INTEGER, account_code TEXT, category_id INTEGER);
        CREATE TABLE account_control_cashflow_categories
            (id INTEGER, client_id INTEGER, label TEXT, is_active INTEGER);
    """)
    rows = [
        # Three source lines form one balanced voucher across correction uploads.
        (1, 7, 9, 12, 0, 'active', 'V1', '2026-01-01', 'BANK', 100, 0, 'bank'),
        (2, 7, 9, 13, 0, 'active', 'V1', '2026-01-01', 'EXP', 0, 50, ''),
        (3, 7, 9, 13, 0, 'active', 'V1', '2026-01-01', 'EXP', 0, 50, ''),
        (4, 7, 9, 12, 0, 'active', 'ZERO', '2026-01-31', 'EXP', 0, 0, ''),
        (5, 7, 9, 12, 0, 'active', 'JV1', '2026-01-15', 'EXP', 10, 0, ''),
        (6, 7, 9, 12, 0, 'active', 'JV1', '2026-01-15', 'OTHER', 0, 10, ''),
        (7, 7, 9, 12, 0, 'active', '   ', '2026-01-15', 'EXP', 0, 0, ''),
        (8, 8, 9, 12, 0, 'active', 'OTHER-CLIENT', '2026-01-01', 'BANK', 1, 0, 'bank'),
        (9, 7, 10, 99, 0, 'active', 'OTHER-TEMPLATE', '2026-01-01', 'BANK', 1, 0, 'bank'),
        (10, 7, 9, 12, 0, 'active', 'BEFORE', '2025-12-31', 'BANK', 1, 0, 'bank'),
        (11, 7, 9, 12, 0, 'active', 'AFTER', '2026-02-01', 'BANK', 1, 0, 'bank'),
        (12, 7, 9, 12, 1, 'active', 'REMOVED', '2026-01-01', 'BANK', 1, 0, 'bank'),
        (13, 7, 9, 12, 0, 'deleted', 'RETIRED', '2026-01-01', 'BANK', 1, 0, 'bank'),
    ]
    conn.executemany("""INSERT INTO client_data
        (id,client_id,template_id,upload_id,is_deleted,change_detection_status,
         derived_voucher_id,derived_transaction_date,derived_account_code,
         derived_debit,derived_credit,derived_treasury_type)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""", rows)
    calls = []

    def query(sql, params=None):
        calls.append((sql, params))
        # The production statement is PostgreSQL; only its cast/driver syntax differs.
        statement = sql.replace("%s", "?").replace("::double precision", "")
        return [dict(row) for row in conn.execute(statement, params or ())]

    yield conn, query, calls
    conn.close()


def test_database_totals_count_source_multiplicity_zeros_and_non_cash_vouchers(database_count_source):
    _conn, query, calls = database_count_source
    source = load_ledger_report_source(7, journal_template_id=9, period_start="2026-01-01",
        period_end="2026-01-31", account_category_source="cashflow", query_fn=query)

    assert source.metadata["database_totals"] == {
        "client_id": 7, "template_id": 9, "upload_id": None,
        "period_start": "2026-01-01", "period_end": "2026-01-31",
        "row_count": 7, "voucher_count": 3,
    }
    assert source.ready
    assert len(source.journal) == 2  # Bank + aggregated expense, not seven source rows.
    assert source.metadata["source_row_count"] == 3
    assert source.journal["debit"].sum() == source.journal["credit"].sum() == 100
    assert len(calls) == 1


def test_database_totals_survive_when_period_contains_no_cashflow_vouchers(database_count_source):
    conn, query, _calls = database_count_source
    conn.execute("DELETE FROM client_data WHERE derived_voucher_id='V1'")
    source = load_ledger_report_source(7, journal_template_id=9, period_start="2026-01-01",
        period_end="2026-01-31", account_category_source="cashflow", query_fn=query)
    assert source.metadata["database_totals"]["row_count"] == 4
    assert source.metadata["database_totals"]["voucher_count"] == 2
    assert source.journal.empty
    assert not source.ready


def test_database_totals_distinguish_empty_period_from_unavailable_counts(database_count_source):
    _conn, query, _calls = database_count_source
    source = load_ledger_report_source(7, journal_template_id=9, period_start="2026-03-01",
        period_end="2026-03-31", account_category_source="cashflow", query_fn=query)
    assert source.metadata["database_totals"]["row_count"] == 0
    assert source.metadata["database_totals"]["voucher_count"] == 0
    assert source.journal.empty


def test_real_source_sql_preserves_repeats_and_moves_corrected_dates_between_years(
        database_count_source, independent_year_boundary_case):
    from services.ledger_group_summary import build_cashflow_summary
    conn, query, _ = database_count_source
    case = independent_year_boundary_case
    conn.execute('DELETE FROM client_data')
    for identity, row in enumerate(case['journal'].to_dict('records'), 1):
        conn.execute('''INSERT INTO client_data
            (id, client_id, template_id, upload_id, is_deleted, change_detection_status,
             derived_voucher_id, derived_transaction_date, derived_account_code, derived_account_name,
             derived_transaction_month, derived_debit, derived_credit, derived_treasury_type, row_data)
            VALUES (?,7,9,12,0,'active',?,?,?,?,?,?,?,?, '{}')''',
            (identity, row['voucher_id'], row['date'], row['account_code'], row['account_name'],
             row['date'][:7], row['debit'], row['credit'],
             case['accounts'].get(row['account_code'], '').lower()))
    def read():
        source = load_ledger_report_source(7, journal_template_id=9, period_start=case['start'],
            period_end=case['end'], account_category_source='cashflow', query_fn=query)
        summary = build_cashflow_summary(source.journal, case['coa'], ['C1'], ['B1', 'B0'],
            case['start'], case['end'], cashflow_role_categories=case['roles'], vendor_account_codes=['AP'])
        return source, {(r['period_key'], r['treasury_type']): r['source_movement']
                        for r in summary['treasury_movement_proof']}
    source, original = read()
    assert source.metadata['database_totals']['row_count'] == 20
    repeats = source.journal[(source.journal.voucher_id == 'REPEATS') & (source.journal.account_code == 'C1')]
    assert repeats['debit'].sum() == pytest.approx(6.66)
    assert conn.execute("SELECT COUNT(*) FROM client_data WHERE derived_voucher_id='REPEATS' AND derived_account_code='C1'").fetchone()[0] == 2
    assert source.metadata['source_row_count'] == 20
    assert original[('2025-12', 'combined')] == pytest.approx(97)
    assert original[('2026-01', 'combined')] == pytest.approx(-40.30)
    # Only this owned in-memory database is changed; both ends of the corrected voucher move.
    conn.execute("UPDATE client_data SET derived_transaction_date='2026-01-01', derived_transaction_month='2026-01' WHERE derived_voucher_id='RECEIPT'")
    _, corrected = read()
    assert corrected[('2025-12', 'combined')] == pytest.approx(-.25)
    assert corrected[('2026-01', 'combined')] == pytest.approx(56.95)
    assert sum(original.values()) == pytest.approx(sum(corrected.values()))
    conn.execute("DELETE FROM client_data WHERE derived_voucher_id='REFUND'")
    _, removed = read()
    assert removed[('2026-01', 'cash')] == pytest.approx(-18.84)
    assert removed[('2026-01', 'bank')] == corrected[('2026-01', 'bank')]


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


def test_load_cashflow_source_requires_derived_client_data_rows():
    rows = [{
        "voucher_id": "V1",
        "date": "2026-01-05",
        "account_code": "B1",
        "account_name": "Bank A",
        "voucher_detail": "Receipt",
        "debit": 100,
        "credit": 0,
        "treasury_type": "bank",
        "category": "",
        "transaction_month": "2026-01",
        "transaction_fortnight": "2026-01-F1",
        "upload_id": 12,
        "source_line_count": 1,
    }]

    def query(sql, params=None):
        if "FROM client_data cd" in sql:
            return rows
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
    assert source.metadata["cashflow_report_source_mode"] == "client_data_derived"
    assert source.journal.loc[0, "account_code"] == "B1"


def test_cashflow_template_query_filters_period_and_uses_active_client_data():
    rows = [{
        "voucher_id": "V1",
        "date": "2026-01-05",
        "account_code": "B1",
        "account_name": "Bank A",
        "voucher_detail": "Receipt",
        "debit": 100,
        "credit": 0,
        "treasury_type": "bank",
        "category": "",
        "transaction_month": "2026-01",
        "transaction_fortnight": "2026-01-F1",
        "upload_id": 12,
        "source_line_count": 1,
    }, {
        "voucher_id": "V1",
        "date": "2026-01-05",
        "account_code": "EXP",
        "account_name": "Expense",
        "voucher_detail": "Receipt",
        "debit": 0,
        "credit": 100,
        "treasury_type": "",
        "category": "Operating",
        "transaction_month": "2026-01",
        "transaction_fortnight": "2026-01-F1",
        "upload_id": 12,
        "source_line_count": 1,
    }]
    calls = []

    def query(sql, params=None):
        calls.append((sql, params))
        if "FROM client_data cd" in sql:
            return rows
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

    source_query, source_params = calls[0]
    assert source.ready
    assert source.metadata["date_prefilter_applied"] is True
    assert source.metadata["duplicate_upload_suppression"] is False
    assert source.metadata["treasury_filter_stage"] == "client_data_derived"
    assert "cd.derived_transaction_date >= %s" in source_query
    assert "cd.derived_transaction_date <= %s" in source_query
    assert source_query.index("cd.template_id = %s") < source_query.index("cd.derived_transaction_date >= %s")
    assert "cash_bank_transaction_classifications" not in source_query
    assert "cashflow_voucher_occurrences" not in source_query
    assert source_params == (7, 9, "2026-01-01", "2026-01-31") * 2

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


def test_corrected_voucher_sql_retains_counterpart_from_previous_upload():
    """Execute the generated query against two uploads, rather than mock its rows."""
    import sqlite3
    from services.ledger_report_source import load_cashflow_client_data_source
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.create_function("BTRIM", 1, lambda value: value.strip() if value else value)
    conn.executescript("""
        CREATE TABLE client_data (
            id INTEGER, client_id INTEGER, template_id INTEGER, upload_id INTEGER,
            is_deleted INTEGER, change_detection_status TEXT, derived_voucher_id TEXT,
            derived_transaction_date TEXT, derived_account_code TEXT, derived_account_name TEXT,
            derived_transaction_month TEXT, derived_transaction_fortnight TEXT,
            row_data TEXT, derived_debit REAL, derived_credit REAL,
            derived_treasury_type TEXT, derived_classification_status TEXT
        );
        CREATE TABLE account_control_cashflow_category_assignments (client_id INTEGER, account_code TEXT, category_id INTEGER);
        CREATE TABLE account_control_cashflow_categories (id INTEGER, client_id INTEGER, is_active INTEGER, label TEXT);
    """)
    conn.executemany("INSERT INTO client_data VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", [
        (1, 5, 1, 20, 0, "active", "V1", "2026-01-05", "B", "Bank", "2026-01", "", "{}", 100, 0, "bank", "classified"),
        (2, 5, 1, 10, 0, "active", "V1", "2026-01-05", "C", "Customer", "2026-01", "", "{}", 0, 100, "", "classified"),
        # A separate client's same voucher number must remain outside the scope.
        (3, 6, 1, 20, 0, "active", "V1", "2026-01-05", "X", "Other", "2026-01", "", "{}", 999, 0, "bank", "classified"),
    ])
    def query(sql, params=None):
        return [dict(row) for row in conn.execute(sql.replace("::double precision", "").replace("%s", "?"), params or ())]
    source = load_cashflow_client_data_source(5, journal_template_id=1, period_start="2026-01-01", period_end="2026-01-31", query_fn=query)
    assert set(source.journal["account_code"]) == {"B", "C"}
    assert source.journal["debit"].sum() == source.journal["credit"].sum() == 100
    conn.close()
