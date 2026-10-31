"""Synthetic Income Statement, shared-profit and confirmation safety checks."""

from copy import deepcopy
from contextlib import nullcontext
from unittest.mock import patch

import pytest
from fastapi import HTTPException

from routers import account_control_center as acc, reconciliation
from services.financial_statement_balances import (
    build_income_statement, build_balance_sheet, financial_statement_context_key,
    BalanceSheetInputError,
)
from test_balance_sheet import account, category, mapping, calc, part


@pytest.fixture
def statement_case():
    categories = [category(100, 'equity', heading=True), category(101, parent=100), category(102, 'asset')]
    categories += [category(key, section='income_statement') for key in (2, 3, 4)]
    accounts = [account('CAP', credit='1000'), account('CASH', debit='1300'), account('REV', credit='1000'), account('COST', debit='600'), account('TAX', debit='100')]
    for row in accounts:
        row.update(opening_dr='0', opening_cr='0', period_dr=row['closing_dr'], period_cr=row['closing_cr'])
    assignments = [mapping('CAP',101), mapping('CASH',102), mapping('REV',2), mapping('COST',3), mapping('TAX',4)]
    calculations = [dict(calc(10), statement_section='income_statement'), dict(calc(11), statement_section='income_statement'), calc(12,'subtotal',100)]
    components = [part(10,2), part(10,3), part(11,source=10), part(11,4)]
    return [accounts,assignments,categories,calculations,components]


def link_options(**extra):
    return dict(profit_link={'source_calculation_id':11,'target_category_id':101},
                closing_status='not_in_capital',context_key='a'*64,confirmed_context_key='a'*64,**extra)


def test_income_signs_formulas_and_independent_trial_balance_reconciliation(statement_case):
    original=deepcopy(statement_case)
    result=build_income_statement(*statement_case,**link_options())
    assert {row['id']:row['amount'] for row in result['categories']}=={2:'1000',3:'-600',4:'-100'}
    assert {row['id']:row['amount'] for row in result['calculations']}=={10:'400',11:'300'}
    assert result['profit_link']['tb_profit']=='300'
    assert result['profit_link']['difference']=='0'
    assert result['profit_link']['applied']
    assert statement_case==original


def test_profit_is_added_to_capital_once_and_totals_use_combined_amount(statement_case):
    result=build_balance_sheet(*statement_case,**link_options())
    capital=next(row for row in result['categories'] if row['id']==101)
    assert (capital['erp_amount'],capital['linked_profit'],capital['amount'])==('1000','300','1300')
    assert result['calculations'][0]['amount']=='1300'
    repeated=build_balance_sheet(*statement_case,**link_options())
    assert repeated==result
    assert statement_case[0][0]['closing_cr']=='1000'


def test_loss_reduces_capital_and_preserves_signed_result(statement_case):
    statement_case[0][2]['closing_cr']='200'
    result=build_balance_sheet(*statement_case,**link_options())
    assert result['profit_link']['final_profit']=='-500'
    assert next(row for row in result['categories'] if row['id']==101)['amount']=='500'


@pytest.mark.parametrize('status,confirmed', [('not_confirmed','a'*64),('already_in_capital','a'*64),('not_in_capital','b'*64),('invalid','a'*64)])
def test_unconfirmed_already_posted_and_stale_confirmations_do_not_add_profit(statement_case,status,confirmed):
    options=link_options();options.update(closing_status=status,confirmed_context_key=confirmed)
    result=build_balance_sheet(*statement_case,**options)
    assert not result['profit_link']['applied']
    assert next(row for row in result['categories'] if row['id']==101)['amount']=='1000'


def test_wrong_final_total_and_nonzero_unmapped_account_block_link(statement_case):
    options=link_options();options['profit_link']['source_calculation_id']=10
    result=build_income_statement(*statement_case,**options)
    assert result['profit_link']['difference']=='100'
    assert not result['profit_link']['applied']
    statement_case[0].append(account('UNKNOWN',debit='1'))
    assert not build_income_statement(*statement_case,**link_options())['profit_link']['applied']
    statement_case[0][-1]['closing_dr']='0'
    assert build_income_statement(*statement_case,**link_options())['profit_link']['applied']


def test_offsetting_omitted_categories_do_not_pass_link_by_net_equality(statement_case):
    statement_case[2].extend([category(5,section='income_statement'),category(6,section='income_statement')])
    statement_case[0].extend([account('OMITTED-DR',debit='50'),account('OMITTED-CR',credit='50')])
    statement_case[1].extend([mapping('OMITTED-DR',5),mapping('OMITTED-CR',6)])
    result=build_income_statement(*statement_case,**link_options())
    assert result['profit_link']['reconciled']
    assert not result['profit_link']['applied']
    assert any('omits' in reason for reason in result['profit_link']['reasons'])


@pytest.mark.parametrize('change', ['inactive','wrong_role','missing_target','missing_source'])
def test_invalid_link_configuration_never_changes_capital(statement_case,change):
    options=link_options()
    if change=='inactive':statement_case[2][1]['is_active']=False
    if change=='wrong_role':statement_case[2][0]['presentation_role']='liability'
    if change=='missing_target':options['profit_link']['target_category_id']=999
    if change=='missing_source':options['profit_link']['source_calculation_id']=999
    result=build_balance_sheet(*statement_case,**options)
    assert not result['profit_link']['applied']


def test_opening_income_balances_are_flagged_without_changing_closing_basis(statement_case):
    statement_case[0][2]['opening_cr']='80'
    result=build_income_statement(*statement_case)
    assert result['controls']['income_statement_net']=='300'
    assert any(row['code']=='opening_profit_balances' for row in result['diagnostics'])


def test_comparative_build_preserves_current_income_and_confirmation(statement_case):
    statement_case[0][3]['opening_dr'] = '20'
    original = deepcopy(statement_case)
    options = link_options()
    before = build_income_statement(*statement_case, **options)
    fingerprint = financial_statement_context_key(*statement_case, options['profit_link'])
    balance = build_balance_sheet(*statement_case, **options)
    assert balance['profit_link']['final_profit'] == '300'
    assert balance['profit_link']['applied']
    assert balance['comparative']['opening_review']['amount'] == '-20'
    assert next(row for row in balance['categories'] if row['id'] == 101)['amount'] == '1300'
    assert build_income_statement(*statement_case, **options) == before
    assert financial_statement_context_key(*statement_case, options['profit_link']) == fingerprint
    assert statement_case == original


def test_context_key_is_order_independent_but_changes_for_source_mapping_and_formula(statement_case):
    key=financial_statement_context_key(*statement_case,link_options()['profit_link'])
    assert key==financial_statement_context_key(*(list(reversed(group)) for group in statement_case),link_options()['profit_link'])
    for group,field,value in [(0,'closing_dr','999'),(1,'financial_statement_category_id',999),(4,'operator','subtract')]:
        changed=deepcopy(statement_case);changed[group][0][field]=value
        assert financial_statement_context_key(*changed,link_options()['profit_link'])!=key


def test_subtracting_negative_expense_is_preserved_flagged_and_does_not_reconcile(statement_case):
    statement_case[4][1]['operator']='subtract'
    result=build_income_statement(*statement_case,**link_options())
    assert result['calculations'][0]['amount']=='1600'
    assert not result['profit_link']['applied']
    assert any(row['code']=='subtract_negative' for row in result['diagnostics'])


def test_config_save_validates_both_sources_and_clear_is_explicit():
    payload=acc.ProfitLinkPayload(client_id=7,source_calculation_id=11,target_category_id=101)
    with patch.object(acc,'_require_active_client'),patch.object(acc,'pg_transaction',side_effect=lambda:nullcontext()),patch.object(acc,'execute_query_pg',side_effect=[[{'id':11}],[{'id':101}]]),patch.object(acc,'execute_write_pg') as write,patch.object(acc,'log_audit'):
        assert acc.save_profit_link(payload,session={'username':'Admin'})['saved']
        assert write.call_args.args[1][:3]==(7,11,101)
    with patch.object(acc,'_require_active_client'),patch.object(acc,'pg_transaction',side_effect=lambda:nullcontext()),patch.object(acc,'execute_query_pg',side_effect=[[],[]]),patch.object(acc,'execute_write_pg') as write:
        with pytest.raises(HTTPException) as error:acc.save_profit_link(payload,session={})
        assert error.value.status_code==400;write.assert_not_called()
    with patch.object(acc,'_require_active_client'),patch.object(acc,'pg_transaction',side_effect=lambda:nullcontext()),patch.object(acc,'execute_write_pg') as write,patch.object(acc,'log_audit'):
        acc.save_profit_link(acc.ProfitLinkPayload(client_id=7),session={})
        assert 'DELETE FROM account_control_profit_links' in write.call_args.args[0]


def test_confirmation_is_client_upload_and_context_bound_and_rejects_stale_state():
    payload=acc.ProfitClosingStatusPayload(client_id=7,trial_balance_upload_id=55,closing_status='not_in_capital',context_key='a'*64)
    with patch.object(reconciliation,'income_statement_report',return_value={'context_key':'a'*64}) as read,patch.object(acc,'execute_write_pg') as write,patch.object(acc,'log_audit'):
        assert acc.save_profit_closing_status(payload,session={'username':'Admin'})['saved']
        assert read.call_args.args[:2]==(7,55)
        assert write.call_args.args[1][:4]==(55,7,'not_in_capital','a'*64)
    with patch.object(reconciliation,'income_statement_report',return_value={'context_key':'b'*64}),patch.object(acc,'execute_write_pg') as write:
        with pytest.raises(HTTPException) as error:acc.save_profit_closing_status(payload,session={})
        assert error.value.status_code==409;write.assert_not_called()


def test_link_sources_are_protected_from_deletion():
    with patch.object(acc,'_require_active_client'),patch.object(acc,'pg_transaction',side_effect=lambda:nullcontext()),patch.object(acc,'execute_query_pg',return_value=[{'client_id':7}]),patch.object(acc,'execute_write_pg') as write:
        with pytest.raises(HTTPException) as error:acc.delete_statement_calculation(11,client_id=7,session={})
        assert error.value.status_code==409
        with pytest.raises(HTTPException) as error:acc.delete_account_control_category(101,client_id=7,session={})
        assert error.value.status_code==409;write.assert_not_called()


def test_new_endpoints_require_admin():
    for router,path in [(acc.router,'/api/account-control-center/profit-link'),(acc.router,'/api/account-control-center/profit-status'),(reconciliation.router,'/api/reconciliation/financial-statements/income-statement'),(reconciliation.router,'/api/reconciliation/financial-statements/notes')]:
        route=next(route for route in router.routes if route.path==path)
        assert acc.require_admin in [dependency.call for dependency in route.dependant.dependencies]


def test_http_statement_confirmation_linking_and_source_change_refusal(statement_case, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    state={'status':'not_confirmed','key':None}
    calls=[]
    def query(sql,params=()):
        calls.append(sql)
        if 'FROM clients' in sql:return [{'id':7}] if params==(7,) else []
        if 'FROM trial_balance_uploads' in sql:
            return [dict(id=55,client_id=7,period_start='2026-01-01',period_end='2026-06-30',file_name='Synthetic.xlsx',validation_controls='{}',closing_status=state['status'],confirmed_context_key=state['key'])] if params==(55,7) else []
        if 'FROM trial_balance_accounts' in sql:return deepcopy(statement_case[0])
        if 'FROM account_control_assignments' in sql:return deepcopy(statement_case[1])
        if 'FROM account_control_categories' in sql:return deepcopy(statement_case[2])
        if 'FROM account_control_statement_calculations' in sql:return deepcopy(statement_case[3])
        if 'FROM account_control_statement_calculation_components' in sql:return deepcopy(statement_case[4])
        if 'FROM trial_balance_rollforward_adjustments' in sql:return [{'count':0}]
        if 'FROM account_control_profit_links' in sql:return [link_options()['profit_link']]
        raise AssertionError(sql)
    def write(sql,params):
        assert 'INSERT INTO trial_balance_profit_status' in sql
        state.update(status=params[2],key=params[3])
    monkeypatch.setattr(reconciliation,'execute_query_pg',query)
    monkeypatch.setattr(reconciliation,'run_read_snapshot_pg',lambda read:read(None))
    monkeypatch.setattr(acc,'execute_write_pg',write)
    monkeypatch.setattr(acc,'log_audit',lambda *args:None)
    app=FastAPI();app.include_router(reconciliation.router);app.include_router(acc.router)
    app.dependency_overrides[acc.require_admin]=lambda:{'role':'admin','username':'Admin'}
    params={'client_id':7,'trial_balance_upload_id':55}
    prefix='/api/reconciliation/financial-statements/'
    with TestClient(app) as client:
        income=client.get(prefix+'income-statement',params=params)
        assert income.status_code==200,income.text
        result=income.json();assert result['statement_section']=='income_statement'
        assert result['profit_link']['final_profit']=='300'
        assert not result['profit_link']['applied']
        assert len(calls)==10
        body={**params,'closing_status':'not_in_capital','context_key':result['context_key']}
        assert client.put('/api/account-control-center/profit-status',json=body).status_code==200
        balance=client.get(prefix+'balance-sheet',params=params).json()
        assert next(row for row in balance['categories'] if row['id']==101)['amount']=='1300'
        body['closing_status']='already_in_capital'
        assert client.put('/api/account-control-center/profit-status',json=body).status_code==200
        balance=client.get(prefix+'balance-sheet',params=params).json()
        assert next(row for row in balance['categories'] if row['id']==101)['amount']=='1000'
        statement_case[0][2]['closing_cr']='1100'
        assert client.put('/api/account-control-center/profit-status',json=body).status_code==409
        balance=client.get(prefix+'balance-sheet',params=params).json()
        assert balance['profit_link']['closing_status']=='not_confirmed'
        assert client.get(prefix+'income-statement',params={**params,'trial_balance_upload_id':999}).status_code==404
        assert client.get(prefix+'income-statement',params={**params,'client_id':999}).status_code==404
        app.dependency_overrides.clear()
        assert client.get(prefix+'income-statement',params=params).status_code==401
