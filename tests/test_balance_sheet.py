"""ERP-only Balance Sheet contracts; all data is synthetic and reads are mocked."""

from copy import deepcopy

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from routers import reconciliation
from services.financial_statement_balances import build_balance_sheet, BalanceSheetInputError


def category(key, role=None, parent=None, section="balance_sheet", heading=False):
    return dict(id=key, label=f"Category {key}", presentation_role=role,
                row_type="heading" if heading else "category", parent_category_id=parent,
                statement_section=section, sort_order=key, is_active=True)


def account(code, debit="0", credit="0", **extra):
    return dict(account_code=code, closing_dr=debit, closing_cr=credit,
                opening_dr=debit, opening_cr=credit, period_dr="0", period_cr="0",
                classification="leaf", manually_overridden=False, **extra)


def mapping(code, key):
    return dict(account_code=code, financial_statement_category_id=key)


def calc(key, kind="total", heading=None):
    return dict(id=key, label=f"Total {key}", calculation_type=kind,
                parent_heading_id=heading, statement_section="balance_sheet", sort_order=key)


def part(key, category_id=None, source=None, operator="add"):
    return dict(calculation_id=key, source_category_id=category_id,
                source_calculation_id=source, operator=operator)


def amounts(result):
    return {row["id"]: row["amount"] for row in result["categories"] if row["row_type"] == "category"}


def test_decimal_source_signs_inheritance_and_abnormal_balances():
    categories = [category(1, "asset", heading=True), category(2, parent=1),
                  category(3, "liability"), category(4, "equity"), category(5, parent=1)]
    accounts = [account("A", "100.004"), account("L", credit="70.001"),
                account("E", credit="20"), account("ALLOW", credit="10.003")]
    result = build_balance_sheet(accounts, [mapping("A", 2), mapping("L", 3), mapping("E", 4), mapping("ALLOW", 5)], categories, [], [])
    assert amounts(result) == {2: "100.004", 3: "70.001", 4: "20", 5: "-10.003"}
    assert result["controls"]["closing_difference"] == "0.000"
    assert "negative_presented_balances" in {row["code"] for row in result["diagnostics"]}


def test_subtotals_use_presented_values_then_operators_and_chained_totals():
    categories = [category(1, "asset", heading=True), category(2, parent=1), category(3, parent=1), category(4, "liability")]
    calculations = [calc(10, "subtotal", 1), calc(11), calc(12)]
    components = [part(11, source=10), part(11, 4), part(12, source=11)]
    result = build_balance_sheet([account("A", "100"), account("D", credit="40"), account("L", credit="60")],
                                [mapping("A", 2), mapping("D", 3), mapping("L", 4)], categories, calculations, components)
    assert {row["id"]: row["amount"] for row in result["calculations"]} == {10: "60", 11: "120", 12: "120"}
    # An explicit ACC subtraction stays explicit, including the review warning.
    result = build_balance_sheet([account("A", "100"), account("D", credit="40")], [mapping("A", 2), mapping("D", 3)],
                                categories, [calc(10, "subtotal", 1)], [part(10, 3, operator="subtract")])
    assert result["calculations"][0]["amount"] == "140"
    assert "subtract_negative" in {row["code"] for row in result["diagnostics"]}


def test_leaf_hierarchy_ignores_parent_amounts_and_honours_manual_override():
    parent = account("1", "9999")
    leaf = account("01-0001", "30")
    override = account("2", "7")
    override.update(classification="parent", manually_overridden=True)
    result = build_balance_sheet([parent, leaf, override], [mapping("1", 1), mapping("01-0001", 1), mapping("2", 1)], [category(1, "asset")], [], [])
    assert amounts(result)[1] == "30"
    assert result["controls"]["leaf_count"] == 1


def test_missing_roles_propagate_unknown_and_zero_is_distinct():
    result = build_balance_sheet([account("A", "10")], [mapping("A", 2)],
                                [category(1, heading=True), category(2, parent=1), category(3, "asset")],
                                [calc(10, "subtotal", 1), calc(11)], [part(11, source=10)])
    assert amounts(result) == {2: None, 3: "0"}
    assert all(row["amount"] is None for row in result["calculations"])
    assert "missing_presentation" in {row["code"] for row in result["diagnostics"]}


def test_existing_automatic_entries_never_change_erp_closing_and_profit_is_not_added():
    accounts = [account("A", "100"), account("REV", credit="25"), account("UNMAPPED", credit="75")]
    accounts[0].update(opening_dr="80", closing_dr_adjustment="9999", audit_dr="9999")
    result = build_balance_sheet(accounts, [mapping("A", 1), mapping("REV", 2)],
                                [category(1, "asset"), category(2, section="income_statement")], [], [],
                                {"global_adjustments": [{"closing_dr_adjustment": "12345"}]}, 1)
    assert amounts(result)[1] == "100"
    assert result["controls"]["income_statement_net"] == "25"
    assert result["controls"]["audit_adjustment_net"] == "0"
    assert result["controls"]["unmapped_account_codes"] == ["UNMAPPED"]
    assert {"excluded_differences", "unclosed_profit_loss", "unmapped_accounts"}.issubset({row["code"] for row in result["diagnostics"]})


def test_missing_unplaced_inactive_and_cross_client_category_ids_do_not_disappear():
    inactive = category(1, "asset"); inactive["is_active"] = False
    unplaced = category(2); unplaced["statement_section"] = None
    result = build_balance_sheet([account("A", "10"), account("B", "20"), account("C", "30")],
                                [mapping("A", 1), mapping("B", 2), mapping("C", 999)], [inactive, unplaced], [], [])
    assert amounts(result)[1] == "10"
    assert result["controls"]["unmapped_count"] == 2
    assert "inactive_assignments" in {row["code"] for row in result["diagnostics"]}


@pytest.mark.parametrize("bad", ["NaN", "Infinity", "not an amount"])
def test_invalid_amounts_refuse_report(bad):
    with pytest.raises(BalanceSheetInputError):
        build_balance_sheet([account("A", bad)], [mapping("A", 1)], [category(1, "asset")], [], [])


@pytest.mark.parametrize("parts", [[part(10, source=10)], [part(10, source=99)], [part(10, 1), part(10, 1)], [part(10, 1, operator="invalid")]])
def test_cycles_missing_sources_overlap_and_invalid_operators_refuse_report(parts):
    with pytest.raises(BalanceSheetInputError):
        build_balance_sheet([account("A", "1")], [mapping("A", 1)], [category(1, "asset")], [calc(10)], parts)


def test_large_amounts_and_repeated_fractions_stay_decimal_and_inputs_unchanged():
    accounts = [account("BIG", "9007199254740993.01")] + [account(str(i), "0.004") for i in range(100)]
    original = deepcopy(accounts)
    result = build_balance_sheet(accounts, [mapping(row["account_code"], 1) for row in accounts], [category(1, "asset")], [], [])
    assert amounts(result)[1] == "9007199254740993.410"
    assert accounts == original


def test_empty_tb_does_not_present_zero_as_complete():
    result = build_balance_sheet([], [], [category(1, "asset")], [], [])
    assert amounts(result)[1] is None
    assert result["diagnostics"][0]["code"] == "no_leaf_accounts"


@pytest.fixture
def report_api(monkeypatch):
    calls = []
    state = {"upload": True, "debit": "25", "mapping": 1, "size": 1}
    def query(sql, params=()):
        calls.append((sql, params))
        if "FROM clients" in sql:
            return [{"id": 7}]
        if "FROM trial_balance_uploads" in sql:
            assert params == (55, 7)
            return [dict(id=55, client_id=7, file_name="Synthetic.xlsx", period_end="2026-06-30", validation_controls="{}")] if state["upload"] else []
        if "FROM trial_balance_accounts" in sql:
            assert params == (55,)
            return [account("A" if state["size"] == 1 else "A" + str(index), state["debit"]) for index in range(state["size"])]
        if "FROM account_control_assignments" in sql:
            assert params == (7,)
            return [mapping("A" if state["size"] == 1 else "A" + str(index), state["mapping"]) for index in range(state["size"])]
        if "FROM account_control_categories" in sql:
            assert params == (7,)
            return [category(1, "asset"), category(2, "liability")]
        if "FROM account_control_statement_calculations" in sql or "FROM account_control_statement_calculation_components" in sql:
            return []
        if "FROM account_control_profit_links" in sql:
            return []
        if "FROM trial_balance_rollforward_adjustments" in sql:
            return [{"count": 1}]
        raise AssertionError(sql)
    monkeypatch.setattr(reconciliation, "execute_query_pg", query)
    snapshots = []
    def snapshot(read):
        snapshots.append(True)
        return read(None)
    monkeypatch.setattr(reconciliation, "run_read_snapshot_pg", snapshot)
    app = FastAPI(); app.include_router(reconciliation.router)
    app.dependency_overrides[reconciliation.require_admin] = lambda: {"user_id": 1, "role": "admin"}
    with TestClient(app) as client:
        yield client, state, calls, snapshots, app


def test_report_api_requires_explicit_upload_and_reads_one_snapshot_without_n_plus_one(report_api):
    client, state, calls, snapshots, _ = report_api
    url = "/api/reconciliation/financial-statements/balance-sheet"
    assert client.get(url, params={"client_id": 7}).status_code == 422
    result = client.get(url, params={"client_id": 7, "trial_balance_upload_id": 55})
    assert result.status_code == 200, result.text
    assert result.json()["categories"][0]["amount"] == "25"
    assert len(calls) == 9 and len(snapshots) == 1
    assert all(sql.lstrip().startswith("SELECT") for sql, _ in calls)
    calls.clear()
    state.update(debit="40", mapping=2, size=50)
    refreshed = client.get(url, params={"client_id": 7, "trial_balance_upload_id": 55}).json()
    assert refreshed["categories"][1]["amount"] == "-2000"
    assert refreshed["controls"]["leaf_count"] == 50
    assert len(calls) == 9


def test_missing_or_foreign_upload_is_refused_without_fallback(report_api):
    client, state, calls, _, _ = report_api
    state["upload"] = False
    response = client.get("/api/reconciliation/financial-statements/balance-sheet", params={"client_id": 7, "trial_balance_upload_id": 55})
    assert response.status_code == 404
    assert len(calls) == 2


def test_report_endpoint_requires_authentication(report_api):
    client, _, calls, _, app = report_api
    app.dependency_overrides.clear()
    response = client.get("/api/reconciliation/financial-statements/balance-sheet", params={"client_id": 7, "trial_balance_upload_id": 55})
    assert response.status_code == 401
    assert not calls


def test_changed_hierarchy_marks_saved_validation_controls_stale():
    result = build_balance_sheet([account('A', '1')], [mapping('A', 1)], [category(1, 'asset')], [], [], {'account_classifications': [['A', 'parent']]})
    assert 'stale_controls' in {row['code'] for row in result['diagnostics']}
