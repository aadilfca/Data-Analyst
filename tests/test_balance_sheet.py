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


@pytest.fixture
def transfer_case():
    return [
        [account('A','100'),account('L',credit='30'),account('C',credit='70')],
        [mapping('A',1),mapping('L',2),mapping('C',3)],
        [category(1,'asset'),category(2,'liability'),category(3,'equity')],
        [calc(10),calc(11)],
        [part(10,1),part(11,2),part(11,3)],
    ]


def transfer(result):
    return result['transfer_reconciliation']


def test_transfer_balanced_source_and_statement_are_green_without_changing_amounts(transfer_case):
    original=deepcopy(transfer_case)
    result=build_balance_sheet(*transfer_case)
    proof=transfer(result)
    assert proof['status']=='reconciled'
    assert proof['equation_balanced']
    assert proof['unexplained_difference']=='0'
    rows={row['key']:row for row in proof['rows']}
    assert rows['asset']['tb']==rows['asset']['bs']=='100'
    assert rows['liability']['tb']==rows['liability']['bs']=='-30'
    assert rows['equity']['tb']==rows['equity']['bs']=='-70'
    assert transfer_case==original
    assert {row['id']:row['amount'] for row in result['calculations']}=={10:'100',11:'100'}


@pytest.mark.parametrize('difference',['-1634.57','0.02','-0.011'])
def test_transfer_unbalanced_erp_can_be_complete_but_does_not_claim_balanced_equation(transfer_case,difference):
    from decimal import Decimal
    transfer_case[0][0]['closing_dr']=str(Decimal('100')+Decimal(difference))
    result=build_balance_sheet(*transfer_case)
    proof=transfer(result)
    assert proof['status']=='reconciled_to_tb'
    assert not proof['equation_balanced']
    assert proof['source_imbalance']==proof['statement_difference']==difference
    assert Decimal(proof['unexplained_difference'])==0
    rows={row['key']:row for row in proof['rows']}
    assert Decimal(rows['source']['tb'])==-Decimal(difference)
    assert rows['unexplained']['tb']=='0'
    assert Decimal(rows['unexplained']['bs'])==0


def test_transfer_uses_unrounded_values_and_existing_tolerance(transfer_case):
    transfer_case[0][0]['closing_dr']='100.004'
    result=build_balance_sheet(*transfer_case)
    assert transfer(result)['status']=='reconciled'
    assert transfer(result)['source_imbalance']=='0.004'
    assert transfer(result)['rows'][0]['tb']=='100.004'
    assert result['calculations'][0]['amount']=='100.004'


def test_transfer_offsetting_unmapped_accounts_never_turn_green(transfer_case):
    transfer_case[0].extend([account('UNMAPPED-DR','25'),account('UNMAPPED-CR',credit='25')])
    proof=transfer(build_balance_sheet(*transfer_case))
    assert proof['source_imbalance']=='0'
    assert proof['statement_difference']=='0'
    assert proof['unexplained_difference']=='0'
    assert proof['status']=='not_reconciled'
    assert any('leaf accounts' in reason for reason in proof['issues'])


def test_transfer_omitted_offsetting_categories_fail_membership_even_when_nets_match(transfer_case):
    transfer_case[0].extend([account('EXTRA-DR','25'),account('EXTRA-CR',credit='25')])
    transfer_case[1].extend([mapping('EXTRA-DR',4),mapping('EXTRA-CR',5)])
    transfer_case[2].extend([category(4,'asset'),category(5,'asset')])
    proof=transfer(build_balance_sheet(*transfer_case))
    assert proof['status']=='not_reconciled'
    assert proof['unexplained_difference'] is None
    assert any('No complete final Assets total' in reason for reason in proof['issues'])


def test_transfer_zero_net_category_with_accounts_cannot_be_omitted(transfer_case):
    transfer_case[0].extend([account('EXTRA-DR','25'),account('EXTRA-CR',credit='25')])
    transfer_case[1].extend([mapping('EXTRA-DR',4),mapping('EXTRA-CR',4)])
    transfer_case[2].append(category(4,'asset'))
    assert transfer(build_balance_sheet(*transfer_case))['status']=='not_reconciled'
    transfer_case[4].append(part(10,4))
    assert transfer(build_balance_sheet(*transfer_case))['status']=='reconciled'


def test_transfer_reversed_zero_net_category_is_not_certified_by_a_zero_difference(transfer_case):
    transfer_case[0].extend([account('EXTRA-DR','25'),account('EXTRA-CR',credit='25')])
    transfer_case[1].extend([mapping('EXTRA-DR',4),mapping('EXTRA-CR',4)])
    transfer_case[2].append(category(4,'asset'))
    transfer_case[4].append(part(10,4,operator='subtract'))
    proof=transfer(build_balance_sheet(*transfer_case))
    assert proof['unexplained_difference']=='0'
    assert proof['status']=='not_reconciled'
    assert any('reverses' in reason for reason in proof['issues'])


def test_transfer_tracks_signs_through_chained_totals(transfer_case):
    transfer_case[3].append(calc(12))
    transfer_case[4].append(part(12,source=10,operator='subtract'))
    proof=transfer(build_balance_sheet(*transfer_case))
    assert proof['status']=='not_reconciled'
    assert any('ambiguous' in reason for reason in proof['issues'])


def test_transfer_never_guesses_totals_from_labels_and_accepts_equivalent_aliases(transfer_case):
    transfer_case[3][0]['label']='An arbitrary label'
    transfer_case[3][1]['label']='Another arbitrary label'
    transfer_case[3].append(calc(12))
    transfer_case[4].append(part(12,source=10))
    proof=transfer(build_balance_sheet(*transfer_case))
    assert proof['status']=='reconciled'
    assert proof['totals']['equity_liabilities']['label']=='Another arbitrary label'


def test_transfer_missing_roles_stale_controls_and_no_leaf_source_stay_red(transfer_case):
    changed=deepcopy(transfer_case);changed[2][0]['presentation_role']=None
    assert transfer(build_balance_sheet(*changed))['status']=='not_reconciled'
    assert transfer(build_balance_sheet(*transfer_case,controls={'stale':True}))['status']=='not_reconciled'
    changed=deepcopy(transfer_case);changed[0]=[];changed[1]=[]
    assert transfer(build_balance_sheet(*changed))['status']=='not_reconciled'


def test_transfer_profit_is_separate_from_capital_and_only_included_once():
    from test_income_statement import statement_case,link_options
    data=statement_case.__wrapped__()
    data[3].extend([calc(13),calc(14)])
    data[4].extend([part(13,102),part(14,source=12)])
    result=build_balance_sheet(*data,**link_options())
    proof=transfer(result)
    assert proof['status']=='reconciled'
    rows={row['key']:row for row in proof['rows']}
    assert rows['equity']['tb']==rows['equity']['bs']=='-1000'
    assert rows['profit']['tb']==rows['profit']['bs']=='-300'
    assert next(row for row in result['categories'] if row['id']==101)['amount']=='1300'
    options=link_options();options['closing_status']='not_confirmed'
    assert transfer(build_balance_sheet(*data,**options))['status']=='not_reconciled'
    options['closing_status']='already_in_capital'
    assert transfer(build_balance_sheet(*data,**options))['status']=='not_reconciled'


def test_transfer_closed_income_accounts_do_not_add_profit_again():
    from test_income_statement import statement_case,link_options
    data=statement_case.__wrapped__()
    data[0][0]['closing_cr']='1300'
    for row in data[0][2:]:row.update(closing_dr='0',closing_cr='0')
    data[3].extend([calc(13),calc(14)]);data[4].extend([part(13,102),part(14,source=12)])
    options=link_options();options['closing_status']='already_in_capital'
    result=build_balance_sheet(*data,**options)
    assert transfer(result)['status']=='reconciled'
    assert next(row for row in result['categories'] if row['id']==101)['amount']=='1300'
    assert next(row for row in transfer(result)['rows'] if row['key']=='profit')['bs']=='0'


def test_transfer_metadata_is_returned_in_existing_snapshot_without_extra_reads(report_api):
    client,_,calls,snapshots,_=report_api
    result=client.get('/api/reconciliation/financial-statements/balance-sheet',params={'client_id':7,'trial_balance_upload_id':55})
    assert result.status_code==200,result.text
    assert result.json()['transfer_reconciliation']['status']=='not_reconciled'
    assert len(calls)==9 and len(snapshots)==1
