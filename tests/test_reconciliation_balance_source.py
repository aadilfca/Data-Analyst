from services.reconciliation_balance_source import (
    build_balance_source,
    load_reconciliation_balance_source,
)


def _rows():
    return [
        {"account_code": "B1", "period_month": "2026-01", "opening_balance": 1000, "actual_closing_balance": 1100},
        {"account_code": "B1", "period_month": "2026-02", "opening_balance": 1000, "actual_closing_balance": 900},
        {"account_code": "B2", "period_month": "2026-01", "opening_balance": 0, "actual_closing_balance": 50},
        {"account_code": "B2", "period_month": "2026-02", "opening_balance": 0, "actual_closing_balance": 0},
    ]


def test_balance_source_returns_engine_maps_and_preserves_zero():
    source = build_balance_source(_rows(), ["B1", "B2"], "2026-01-01", "2026-02-28")

    assert source.ready
    assert source.months == ("2026-01", "2026-02")
    assert source.opening_balances == {"B1": 1000.0, "B2": 0.0}
    assert source.actual_closing_balances["B2"]["2026-02"] == 0.0


def test_balance_source_reports_missing_accounts_months_and_not_zero_substitution():
    source = build_balance_source(
        [{"account_code": "B1", "period_month": "2026-01", "opening_balance": 1000, "actual_closing_balance": 1100}],
        ["B1", "B2"],
        "2026-01-01",
        "2026-02-28",
    )

    assert not source.ready
    codes = {item["code"] for item in source.blocking_diagnostics()}
    assert "missing_closing_balance" in codes
    assert "missing_opening_balance" in codes
    assert "B2" not in source.opening_balances


def test_balance_source_rejects_duplicate_and_inconsistent_values():
    rows = _rows()
    rows.append({"account_code": "B1", "period_month": "2026-01", "opening_balance": 1000, "actual_closing_balance": 1100})
    rows[1]["opening_balance"] = 999
    source = build_balance_source(rows, ["B1", "B2"], "2026-01", "2026-02")

    codes = {item["code"] for item in source.blocking_diagnostics()}
    assert "duplicate_balance_month" in codes
    assert "inconsistent_opening_balance" in codes


def test_balance_source_loader_uses_client_and_group_selector():
    calls = []

    def query(sql, params=None):
        calls.append((sql, params))
        return _rows()

    source = load_reconciliation_balance_source(
        7,
        "Bank",
        ["B1", "B2"],
        "2026-01-01",
        "2026-02-28",
        query_fn=query,
    )

    assert source.ready
    assert calls[0][1] == (7, "Bank")
    assert "report_balance_inputs" in calls[0][0]
