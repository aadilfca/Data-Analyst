"""Isolated contracts for Trial Balance, ACC, Verification, and report data flow."""

from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd
import pytest

from fastapi import HTTPException

from routers import account_control_center, audit_rules, reconciliation, trial_balance
from services.ledger_report_source import _load_account_rows


AMOUNT_FIELDS = (
    "opening_dr",
    "opening_cr",
    "period_dr",
    "period_cr",
    "closing_dr",
    "closing_cr",
)


def test_trial_balance_retains_source_amounts_but_acc_mapping_excludes_them():
    source_accounts = [
        {
            "id": 1,
            "account_code": "100",
            "source_account_code": "100",
            "account_name": "Assets",
            "level": 1,
            "classification": "parent",
            "manually_overridden": False,
            "opening_dr": 900,
            "opening_cr": 0,
            "period_dr": 0,
            "period_cr": 0,
            "closing_dr": 900,
            "closing_cr": 0,
        },
        {
            "id": 2,
            "account_code": "100-01",
            "source_account_code": "100-01",
            "account_name": "Cash",
            "level": 2,
            "classification": "leaf",
            "manually_overridden": False,
            "opening_dr": 100,
            "opening_cr": 0,
            "period_dr": 25,
            "period_cr": 5,
            "closing_dr": 120,
            "closing_cr": 0,
        },
    ]
    with patch.object(trial_balance, "_require_upload", return_value={}), patch.object(
        trial_balance, "execute_query_pg", side_effect=[source_accounts, []]
    ):
        saved = trial_balance.list_trial_balance_accounts(upload_id=55)

    imported_leaf = next(row for row in saved["accounts"] if row["account_code"] == "100-01")
    assert {field: imported_leaf[field] for field in AMOUNT_FIELDS} == {
        "opening_dr": 100,
        "opening_cr": 0,
        "period_dr": 25,
        "period_cr": 5,
        "closing_dr": 120,
        "closing_cr": 0,
    }

    mapping_tree = [
        {
            "id": 2,
            "account_code": "100-01",
            "account_name": "Cash",
            "parent_account_code": "100",
            "level": 2,
            "classification": "leaf",
            **{field: 999 for field in AMOUNT_FIELDS},
        }
    ]
    with patch.object(account_control_center, "_selected_control_accounts", return_value=({"id": 55}, mapping_tree)), patch.object(
        account_control_center,
        "execute_query_pg",
        return_value=[{
            "account_code": "100-01",
            "verification_workstream": "cash",
            "financial_statement_category_id": 31,
            "note_category_id": 42,
        }],
    ), patch.object(account_control_center, "_control_upload_options", return_value=[]):
        mapping = account_control_center.get_account_control_mapping(
            client_id=7, trial_balance_upload_id=55, session={"username": "Admin"}
        )

    mapped_account = mapping["accounts"][0]
    assert mapped_account["account_code"] == "100-01"
    assert mapped_account["assignment"] == {
        "verification_workstream": "cash",
        "financial_statement_category_id": 31,
        "note_category_id": 42,
    }
    assert not set(AMOUNT_FIELDS).intersection(mapped_account)


def test_acc_rejects_upload_from_another_client_or_unparsed_upload():
    with patch.object(account_control_center, "_require_active_client"), patch.object(
        account_control_center, "execute_query_pg", return_value=[]
    ) as query:
        try:
            account_control_center._selected_control_accounts(client_id=7, upload_id=900)
        except HTTPException as error:
            assert error.status_code == 400
        else:
            raise AssertionError("A missing, foreign-client, or unparsed upload was accepted")

    sql, params = query.call_args.args
    assert "WHERE id = %s AND client_id = %s AND status = 'parsed'" in sql
    assert params == (900, 7)


@pytest.mark.parametrize(
    "target_assignment, expected_status",
    [
        (None, "copied"),
        ({"verification_workstream": "bank", "financial_statement_category_id": 32, "note_category_id": None}, 409),
    ],
)
def test_account_code_correction_carries_assignments_without_overwriting_conflicts(target_assignment, expected_status):
    source_assignment = {
        "verification_workstream": "cash",
        "financial_statement_category_id": 31,
        "note_category_id": 41,
    }

    def query(sql, params):
        if "FROM account_control_assignments" in sql:
            return [source_assignment] if params == (7, "OLD-1") else ([target_assignment] if target_assignment else [])
        return []

    with patch.object(trial_balance, "execute_query_pg", side_effect=query), patch.object(
        trial_balance, "execute_write_pg"
    ) as write:
        if expected_status == 409:
            with pytest.raises(HTTPException) as raised:
                trial_balance._copy_shared_code_assignments(7, "OLD-1", "NEW-1")
            assert raised.value.status_code == expected_status
            assert not any("account_control_assignments" in call.args[0] for call in write.call_args_list)
        else:
            trial_balance._copy_shared_code_assignments(7, "OLD-1", "NEW-1")
            copied = next(call for call in write.call_args_list if "account_control_assignments" in call.args[0])
            assert "INSERT INTO account_control_assignments" in copied.args[0]
            assert copied.args[1] == (7, "NEW-1", "cash", 31, 41)


def test_financial_statement_uses_acc_categories_and_surfaces_unmapped_ledger_activity():
    queries = []

    def query(sql, params):
        queries.append((sql, params))
        if "FROM trial_balance_accounts a" in sql:
            return [
                {"account_code": "EXP-1", "account_name": "Office expense", "source_account_type": "Expense"},
                {"account_code": "CASH-1", "account_name": "Cash", "source_account_type": "Cash"},
            ]
        if "FROM account_control_assignments a" in sql:
            return [{"account_code": "EXP-1", "category_name": "Operating expenses"}]
        raise AssertionError(f"Unexpected report account query: {sql}")

    accounts = _load_account_rows(7, query, [], category_source="financial_statement")
    assert next(row for row in accounts if row["account_code"] == "EXP-1")["category"] == "Operating expenses"
    assert next(row for row in accounts if row["account_code"] == "CASH-1")["category"] == ""
    assignment_sql, assignment_params = queries[1]
    assert "a.financial_statement_category_id" in assignment_sql
    assert "c.category_type = 'financial_statement'" in assignment_sql
    assert "c.is_active = TRUE" in assignment_sql
    assert assignment_params == (7,)

    source = SimpleNamespace(
        ready=True,
        diagnostics=(),
        metadata={"source_line_count": 2},
        journal=pd.DataFrame([
            {"voucher_id": "V1", "date": "2026-06-15", "account_code": "EXP-1", "account_name": "Office expense", "debit": 125, "credit": 0},
            {"voucher_id": "V2", "date": "2026-06-18", "account_code": "NEW-1", "account_name": "New account", "debit": 25, "credit": 0},
        ]),
        coa=pd.DataFrame(accounts),
    )
    with patch.object(reconciliation, "_require_active_client"), patch.object(
        reconciliation, "load_ledger_report_source", return_value=source
    ) as load_source:
        result = reconciliation._build_financial_statement_result(
            7, "2026-06-01", "2026-06-30", journal_upload_id=81
        )

    assert load_source.call_args.kwargs["account_category_source"] == "financial_statement"
    assert result["controls"]["financial_statement_mapping"]["status"] == "Review"
    assert result["preview"]["category_totals"][0]["2026-06"] == 125
    assert result["preview"]["unmapped_account_count"] == 1
    assert result["preview"]["unmapped_accounts"][0]["account_code"] == "NEW-1"


def test_verification_category_resolution_prefers_latest_trial_balance_then_uses_fallback():
    def query(sql, params):
        if "FROM trial_balance_uploads" in sql:
            return [{"id": 300}]
        if "FROM trial_balance_accounts a" in sql:
            return [{"account_code": "TB-CODE", "category_id": 51, "category_name": "TB Category"}]
        if "FROM account_category_assignments aca" in sql:
            assert params == (7, "OLD-CODE")
            return [{"account_code": "OLD-CODE", "category_id": 52, "category_name": "Carried category"}]
        raise AssertionError(f"Unexpected category query: {sql}")

    with patch.object(audit_rules, "execute_query_pg", side_effect=query):
        resolved = audit_rules.load_client_account_category_index(7, ["TB-CODE", "OLD-CODE"])

    assert resolved == {
        "TB-CODE": {"category_id": 51, "category_name": "TB Category"},
        "OLD-CODE": {"category_id": 52, "category_name": "Carried category"},
    }


def test_balance_sheet_uses_leaf_closing_fields_without_changing_acc_metadata_contract():
    from services.financial_statement_balances import build_balance_sheet
    account = dict(account_code='100', classification='leaf', manually_overridden=False,
                   closing_dr='120.50', closing_cr='0', opening_dr='100', opening_cr='0', period_dr='20.50', period_cr='0')
    category = dict(id=31, label='Cash', row_type='category', parent_category_id=None,
                    statement_section='balance_sheet', presentation_role='asset', is_active=True)
    result = build_balance_sheet([account], [dict(account_code='100', financial_statement_category_id=31)], [category], [], [])
    assert result['categories'][0]['amount'] == '120.50'
    assert result['controls']['audit_adjustment_net'] == '0'
    assert account['closing_dr'] == '120.50'


@pytest.mark.parametrize('section',['balance_sheet','income_statement'])
def test_paired_assignment_save_reaches_statement_and_notes_without_source_writes(section):
    from contextlib import nullcontext
    from copy import deepcopy
    from decimal import Decimal
    from services.financial_statement_balances import build_balance_sheet,build_income_statement,build_statement_notes
    source=[dict(account_code='A',classification='leaf',manually_overridden=False,opening_dr='100',opening_cr='0',
                 period_dr='0.50',period_cr='0',closing_dr='100.50',closing_cr='0')]
    original=deepcopy(source)
    persisted={'account_code':'A','financial_statement_category_id':7,'note_category_id':11,'verification_workstream':'cash'}
    categories=[dict(id=key,label=label,row_type='category',parent_category_id=None,statement_section=section,
                     presentation_role='asset' if section=='balance_sheet' else None,is_active=True,sort_order=key,category_type='financial_statement')
                for key,label in [(7,'Original category'),(8,'New category')]]
    notes=[dict(id=key,label=label,parent_category_id=None,source_statement_category_id=fs,is_active=True,category_type='note',row_type='category')
           for key,label,fs in [(11,'Original Note',7),(12,'New Note',8)]]
    builder=build_balance_sheet if section=='balance_sheet' else build_income_statement
    before=builder(source,[persisted],categories,[],[])
    before_notes=build_statement_notes(source,[persisted],notes,before,section)

    def query(sql,params):
        if 'pg_advisory_xact_lock' in sql:return []
        if 'FOR UPDATE' in sql:return [dict(persisted)]
        if "category_type IN ('financial_statement', 'note')" in sql:return categories+notes
        if 'SELECT id, row_type' in sql:return [row for row in categories+notes if row['id']==params[0] and row['category_type']==params[2]]
        raise AssertionError('Unexpected query in hermetic account/report flow')

    def save(assignment,actor):
        for field in assignment['_provided_fields']:persisted[field]=assignment[field]

    with patch.object(account_control_center,'_selected_control_accounts',return_value=({'id':77},source)), patch.object(
        account_control_center,'execute_query_pg',side_effect=query), patch.object(account_control_center,'pg_transaction',return_value=nullcontext()), patch.object(
        account_control_center,'_upsert_control_assignment',side_effect=save) as write:
        account_control_center.save_account_control_mapping(account_control_center.AccountControlAssignmentPayload(
            client_id=1,trial_balance_upload_id=77,account_code='A',financial_statement_category_id=8,note_category_id=12),session={'username':'Admin'})
    after=builder(source,[persisted],categories,[],[])
    after_notes=build_statement_notes(source,[persisted],notes,after,section)
    old_amount=next(row['amount'] for row in before['categories'] if row['id']==7)
    assert Decimal(next(row['amount'] for row in after['categories'] if row['id']==8))==Decimal(old_amount)
    assert Decimal(next(row['amount'] for row in after['categories'] if row['id']==7))==0
    assert Decimal(next(row['current_amount'] for row in before_notes['roots'] if row['id']==11))==Decimal(old_amount)
    assert Decimal(next(row['current_amount'] for row in after_notes['roots'] if row['id']==12))==Decimal(old_amount)
    assert source==original and persisted['verification_workstream']=='cash'
    assert write.call_args.args[0]['_provided_fields']=={'financial_statement_category_id','note_category_id'}


@pytest.fixture
def correction_store(monkeypatch):
    from contextlib import contextmanager
    rows=[dict(id=1,account_code='BAD',classification='parent',manually_overridden=True),
          dict(id=2,account_code='10-01',classification='leaf',manually_overridden=False),
          dict(id=3,account_code='BAD2',classification='leaf',manually_overridden=False)]
    state={'rows':rows,'writes':[],'copies':[],'rolled_back':False}
    @contextmanager
    def transaction():
        before=len(state['writes'])
        try: yield
        except Exception:
            state['writes'][before:]=[];state['rolled_back']=True;raise
    def query(sql,params):
        if 'FROM trial_balance_uploads' in sql: return [dict(id=59,client_id=5,status='parsed')]
        if 'FROM trial_balance_accounts' in sql: return state['rows']
        raise AssertionError('Unmocked database query')
    monkeypatch.setattr(trial_balance,'pg_transaction',transaction)
    monkeypatch.setattr(trial_balance,'execute_query_pg',query)
    monkeypatch.setattr(trial_balance,'execute_write_pg',lambda sql,params:state['writes'].append((sql,params)))
    monkeypatch.setattr(trial_balance,'_copy_shared_code_assignments',lambda *args:state['copies'].append(args))
    monkeypatch.setattr(trial_balance,'invalidate_category_for_account_cache',lambda *args:None)
    monkeypatch.setattr(trial_balance,'log_audit',lambda *args:None)
    return state


def test_code_batch_rebuilds_hierarchy_once_and_preserves_overrides(correction_store):
    corrections=[trial_balance.AccountCodeCorrection(account_id=1,account_code='10',expected_account_code='BAD'),
                 trial_balance.AccountCodeCorrection(account_id=3,account_code='20',expected_account_code='BAD2')]
    result=trial_balance.update_trial_balance_account_codes(59,trial_balance.AccountCodeBatchRequest(corrections=corrections))
    assert result['corrected_count']==2
    assert correction_store['copies']==[(5,'BAD','10',59),(5,'BAD2','20',59)]
    sql,params=correction_store['writes'][0]
    assert params==(1,'10',None,1,'parent',2,'10-01','10',2,'leaf',3,'20',None,1,'leaf',59)
    assert 'IS DISTINCT FROM' in sql
    assert all(field not in sql for field in AMOUNT_FIELDS)
    assert 'category_id' not in sql
    assert len(correction_store['writes'])==1


@pytest.mark.parametrize('corrections',[
    [(1,'10','BAD'),(3,'10','BAD2')], # duplicate final codes
    [(1,'10-01','BAD')], # existing account
    [(1,'10','STALE')], # concurrent correction
    [(999,'10','BAD')], # wrong upload/account
    [(1,'','BAD')],
])
def test_code_batch_rejects_whole_draft_before_copy_or_update(correction_store,corrections):
    items=[trial_balance.AccountCodeCorrection(account_id=i,account_code=c,expected_account_code=o) for i,c,o in corrections]
    with pytest.raises(HTTPException): trial_balance._save_account_code_corrections(59,items)
    assert correction_store['copies']==[]
    assert correction_store['writes']==[]
    assert correction_store['rolled_back']


def test_code_batch_large_upload_has_bounded_updates(correction_store):
    correction_store['rows'].extend(dict(id=i,account_code=f'10-{i}',classification='leaf',manually_overridden=False)
                                   for i in range(4,1007))
    trial_balance._save_account_code_corrections(59,[trial_balance.AccountCodeCorrection(
        account_id=1,account_code='10',expected_account_code='BAD')])
    assert len(correction_store['writes'])==2
    assert sum((len(params)-1)//5 for _,params in correction_store['writes'])==1006


def test_code_batch_mapping_conflict_rolls_back_all_changes(correction_store,monkeypatch):
    def copy(client,old,new,upload):
        trial_balance.execute_write_pg('simulated mapping copy',(old,new))
        if old=='BAD2': raise HTTPException(409,'Mapping conflict')
    monkeypatch.setattr(trial_balance,'_copy_shared_code_assignments',copy)
    with pytest.raises(HTTPException):
        trial_balance._save_account_code_corrections(59,[
            trial_balance.AccountCodeCorrection(account_id=1,account_code='10',expected_account_code='BAD'),
            trial_balance.AccountCodeCorrection(account_id=3,account_code='20',expected_account_code='BAD2')])
    assert correction_store['writes']==[]
    assert correction_store['rolled_back']


def test_import_unlinked_parent_warning_and_batch_corrections():
    from io import BytesIO
    frame=pd.DataFrame([dict(Code='BAD',Name='Parent',OD=0,OC=0,PD=0,PC=0,CD=0,CC=0),
                        dict(Code='10-01',Name='Leaf',OD=10,OC=0,PD=0,PC=0,CD=10,CC=0)])
    out=BytesIO();frame.to_excel(out,index=False)
    mapping=dict(account_code_column='Code',account_name_column='Name',opening_dr_column='OD',opening_cr_column='OC',
                 period_dr_column='PD',period_cr_column='PC',closing_dr_column='CD',closing_cr_column='CC')
    result=trial_balance.validate_trial_balance_contents(out.getvalue(),'tb.xlsx',mapping,
        classification_overrides={'2':'parent'},no_total_row=True)
    assert any('Parent has no linked child' in issue['message'] for issue in result['warnings'])
    result=trial_balance.validate_trial_balance_contents(out.getvalue(),'tb.xlsx',mapping,
        code_overrides={'2':'10'},classification_overrides={'2':'parent'},no_total_row=True)
    assert not any('Parent has no linked child' in issue['message'] for issue in result['warnings'])
    assert result['valid']


@pytest.mark.parametrize('table,source,empty', [
    ('account_control_assignments',
     dict(verification_workstream=None,financial_statement_category_id=4,note_category_id=None),
     dict(verification_workstream=None,financial_statement_category_id=None,note_category_id=None)),
    ('account_control_assignments',
     dict(verification_workstream='cash',financial_statement_category_id=4,note_category_id=7),
     dict(verification_workstream=None,financial_statement_category_id=None,note_category_id=None)),
    ('account_category_assignments',dict(category_id=5),dict(category_id=None)),
    ('account_control_cashflow_category_assignments',dict(category_id=6),dict(category_id=None)),
    ('account_control_cashflow_assignments',
     dict(section='investing',category_label='Investments',cash_bank_role='none',treatment_type='standard'),
     dict(section='unassigned',category_label=None,cash_bank_role='none',treatment_type='standard')),
])
def test_code_correction_fills_saved_unassigned_row(table,source,empty):
    def query(sql,params):
        if f'FROM {table} WHERE' in sql:
            return [source if params[1]=='1940-10-10' else empty]
        return []
    with patch.object(trial_balance,'execute_query_pg',side_effect=query), patch.object(
        trial_balance,'execute_write_pg',return_value='10-10-40') as write:
        trial_balance._copy_shared_code_assignments(5,'1940-10-10','10-10-40')
    assert write.call_count==1
    sql,params=write.call_args.args
    assert sql.startswith(f'UPDATE {table} SET')
    assert 'IS NOT DISTINCT FROM' in sql and 'RETURNING account_code' in sql
    assert params==(*source.values(),5,'10-10-40',*empty.values())
    # The source mapping is retained for other uploads, and no source figures are written.
    assert all(field not in sql for field in AMOUNT_FIELDS)


@pytest.mark.parametrize('target',[
    dict(verification_workstream='bank',financial_statement_category_id=None,note_category_id=None),
    dict(verification_workstream=None,financial_statement_category_id=8,note_category_id=None),
    dict(verification_workstream=None,financial_statement_category_id=None,note_category_id=7),
])
def test_code_correction_nonempty_target_still_blocks(target):
    source=dict(verification_workstream=None,financial_statement_category_id=4,note_category_id=7)
    def query(sql,params):
        if 'FROM account_control_assignments WHERE' in sql:
            return [source if params[1]=='OLD' else target]
        return []
    with patch.object(trial_balance,'execute_query_pg',side_effect=query), patch.object(trial_balance,'execute_write_pg') as write:
        with pytest.raises(HTTPException) as raised:
            trial_balance._copy_shared_code_assignments(5,'OLD','10-10-40')
        assert raised.value.status_code==409
        write.assert_not_called()


def test_code_correction_does_not_overwrite_target_changed_after_read():
    source=dict(verification_workstream=None,financial_statement_category_id=4,note_category_id=None)
    empty={key:None for key in source}
    def query(sql,params):
        if 'FROM account_control_assignments WHERE' in sql:
            return [source if params[1]=='OLD' else empty]
        return []
    with patch.object(trial_balance,'execute_query_pg',side_effect=query), patch.object(
        trial_balance,'execute_write_pg',return_value=None) as write:
        with pytest.raises(HTTPException) as raised:
            trial_balance._copy_shared_code_assignments(5,'OLD','10-10-40')
        assert raised.value.status_code==409
        assert 'changed while correcting' in raised.value.detail
        assert write.call_count==1


@pytest.mark.parametrize('same',[False,True])
def test_code_correction_empty_source_and_identical_mapping_are_noops(same):
    real=dict(verification_workstream=None,financial_statement_category_id=4,note_category_id=7)
    empty={key:None for key in real}
    def query(sql,params):
        if 'FROM account_control_assignments WHERE' in sql:
            return [real if same or params[1]=='NEW' else empty]
        return []
    with patch.object(trial_balance,'execute_query_pg',side_effect=query), patch.object(trial_balance,'execute_write_pg') as write:
        trial_balance._copy_shared_code_assignments(5,'OLD','NEW')
        write.assert_not_called()
