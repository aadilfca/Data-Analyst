"""Account ledger source controls; no real workbook upload or database access."""
import copy
import json
import sqlite3
from contextlib import contextmanager, nullcontext
from decimal import Decimal
from datetime import datetime
from unittest.mock import patch

import pandas as pd
import openpyxl
import pytest
from fastapi import HTTPException

import database
from routers import import_export as upload
from routers import templates


ROLES = {role: index for index, role in enumerate(
    ["voucher", "date", "description", "debit", "credit", "balance"], 1)}
COLUMNS = [{"id": index, "column_name": name, "data_type": kind}
           for index, (name, kind) in enumerate([
               ("Reference", "Text"), ("Posting day", "Date"), ("Narrative", "Text"),
               ("Money in", "Numeric"), ("Money out", "Numeric"), ("Book balance", "Text"),
           ], 1)]
TEMPLATE = {"id": 7, "data_purpose": "account_ledger", "ledger_columns": json.dumps(ROLES)}


def request(**changes):
    values = dict(client_id=3, template_id=7, file_id=20, sheet_name="Ledger",
                  ledger_account_code="A-1", ledger_period_start="2026-01-01",
                  ledger_period_end="2026-01-31", date_delimiter="-", date_order="DMY",
                  mappings=[dict(template_column_id=c["id"], template_column_name=c["column_name"], excel_column_name=c["column_name"])
                            for c in COLUMNS])
    values.update(changes)
    return upload.ImportRequest(**values)


def resolve(req=None, account=None):
    with patch.object(upload, "execute_query_pg", side_effect=[
        [account or dict(account_name="Cash A", classification="leaf")], COLUMNS,
    ]):
        return upload._resolve_ledger_context(req or request(), TEMPLATE)


@contextmanager
def ledger(context=None):
    value = resolve() if context is None else context
    token = upload._ledger_context.set(value)
    try:
        yield value
    finally:
        upload._ledger_context.reset(token)


def row(voucher="V1", day="05-01-2026", text="Payment", debit=0, credit=10, balance="90 DR"):
    return dict(zip([c["column_name"] for c in COLUMNS], [voucher, day, text, debit, credit, balance]))


def test_sqlite_fresh_and_existing_metadata_migration_is_idempotent():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE templates (id INTEGER PRIMARY KEY)")
    conn.execute("CREATE TABLE uploaded_files (id INTEGER PRIMARY KEY, client_id INTEGER, template_id INTEGER, status TEXT)")
    conn.execute("INSERT INTO templates VALUES (1)")
    database._ensure_account_ledger_schema(conn)
    database._ensure_account_ledger_schema(conn)
    assert conn.execute("SELECT data_purpose, ledger_columns FROM templates").fetchone() == ("standard", "{}")
    columns = {item[1]: item[2] for item in conn.execute("PRAGMA table_info(uploaded_files)")}
    assert columns["ledger_opening_balance"] == "NUMERIC"
    assert "ledger_account_code" in columns and "ledger_period_end" in columns
    assert conn.execute("SELECT count(*) FROM sqlite_master WHERE name='idx_uploaded_files_ledger_scope'").fetchone()[0] == 1
    conn.close()


def test_postgres_upgrade_uses_only_additive_metadata_and_index():
    from unittest.mock import MagicMock
    conn = MagicMock()
    database._ensure_account_ledger_schema(conn, postgres=True)
    statements = [call.args[0] for call in conn.cursor.return_value.execute.call_args_list]
    assert any("templates ADD COLUMN IF NOT EXISTS data_purpose" in sql for sql in statements)
    assert any("uploaded_files ADD COLUMN IF NOT EXISTS ledger_evidence" in sql for sql in statements)
    assert not any("DELETE" in sql or "DROP" in sql or "CREATE TABLE" in sql for sql in statements)


def test_full_sqlite_bootstrap_contains_ledger_metadata(tmp_path, monkeypatch):
    path = tmp_path / "isolated-ledger.db"
    monkeypatch.setattr(database, "DB_NAME", str(path))
    database.init_db()
    with sqlite3.connect(path) as conn:
        assert "data_purpose" in {row[1] for row in conn.execute("PRAGMA table_info(templates)")}
        assert "ledger_evidence" in {row[1] for row in conn.execute("PRAGMA table_info(uploaded_files)")}


def test_ledger_roles_use_column_ids_and_types_not_client_or_column_names():
    with patch.object(templates, "execute_query_pg", return_value=COLUMNS):
        assert templates.validate_ledger_template_configuration("account_ledger", ROLES, list(ROLES.values())) == ROLES
    assert templates.validate_ledger_template_configuration("standard", {}, [1]) == {}


@pytest.mark.parametrize("roles,selected", [
    ({}, [1]), ({**ROLES, "credit": 4}, list(ROLES.values())), (ROLES, [2, 1, 3, 4, 5, 6]),
    (ROLES, [1, 2, 3, 4, 5]),
])
def test_invalid_ledger_role_mapping_is_rejected_before_writes(roles, selected):
    with patch.object(templates, "execute_write_pg") as write, pytest.raises(HTTPException):
        templates.validate_ledger_template_configuration("account_ledger", roles, selected)
    write.assert_not_called()


def test_wrong_shared_column_type_is_rejected():
    columns = copy.deepcopy(COLUMNS)
    columns[3]["data_type"] = "Text"
    with patch.object(templates, "execute_query_pg", return_value=columns), pytest.raises(HTTPException):
        templates.validate_ledger_template_configuration("account_ledger", ROLES, list(ROLES.values()))


def test_existing_template_editor_preserves_ledger_configuration_when_fields_are_omitted():
    existing = {"id": 7, "data_purpose": "account_ledger", "ledger_columns": json.dumps(ROLES)}
    update = templates.TemplateUpdate(template_name="Renamed ledger", selected_columns=list(ROLES.values()))
    with patch.object(templates, "pg_transaction", side_effect=lambda: nullcontext()), \
            patch.object(templates, "execute_query_pg", side_effect=[[existing], [], COLUMNS]), \
            patch.object(templates, "execute_write_pg") as write, patch.object(templates, "log_audit"), \
            patch.object(templates, "invalidate_template_cache"):
        templates.update_template(7, update)
    first = write.call_args_list[0].args
    assert "data_purpose = %s" in first[0]
    assert first[1][3] == "account_ledger" and json.loads(first[1][4]) == ROLES


@pytest.mark.parametrize("transaction_rows,control_uploads", [([{ "id": 101}], []), ([], [{"id": 20}])])
def test_imported_transactions_or_balance_only_evidence_prevent_purpose_changes(transaction_rows, control_uploads):
    existing = {"id": 7, "data_purpose": "account_ledger", "ledger_columns": json.dumps(ROLES)}
    update = templates.TemplateUpdate(template_name="Ledger", selected_columns=list(ROLES.values()),
                                      data_purpose="standard", ledger_columns={})
    with patch.object(templates, "pg_transaction", side_effect=lambda: nullcontext()), \
            patch.object(templates, "execute_query_pg", side_effect=[[existing], [], transaction_rows, control_uploads]), \
            patch.object(templates, "execute_write_pg") as write, pytest.raises(HTTPException) as error:
        templates.update_template(7, update)
    assert error.value.status_code == 409
    write.assert_not_called()


@pytest.mark.parametrize("changes", [
    {"ledger_account_code": ""}, {"ledger_period_start": None},
    {"ledger_period_end": "2025-12-31"}, {"ledger_period_end": "2026-02-30"},
    {"force_include_rows": [3]},
])
def test_account_coverage_and_override_errors_are_rejected(changes):
    with patch.object(upload, "execute_query_pg") as query, pytest.raises(HTTPException):
        upload._resolve_ledger_context(request(**changes), TEMPLATE)
    query.assert_not_called()


def test_account_selection_requires_current_client_trial_balance_posting_account():
    with patch.object(upload, "execute_query_pg", return_value=[]) as query, pytest.raises(HTTPException):
        upload._resolve_ledger_context(request(), TEMPLATE)
    sql, params = query.call_args.args
    assert "classification = 'leaf'" in sql and "status = 'parsed'" in sql
    assert 'treasury' not in sql and params == ("A-1", 3)


def test_non_treasury_ledger_account_is_accepted_without_cash_bank_configuration():
    account = dict(account_name="Trade payable", classification="leaf")
    context = resolve(account=account)
    assert context['account_code'] == 'A-1' and context['account_name'] == 'Trade payable'


@pytest.mark.parametrize("source,expected", [
    ("1,234.56 DR", Decimal("1234.56")), ("1,234.56 CR", Decimal("-1234.56")),
    ("0.00 CR", Decimal(0)), (-10, Decimal(-10)), (1e-7, Decimal("0.0000001")), (None, None), ("", None),
])
def test_signed_balance_controls_preserve_zero_and_missing(source, expected):
    assert upload._ledger_decimal(source, balance=True) == expected


@pytest.mark.parametrize("source", ["NaN", "Infinity", "1,23 DR", "-10 DR", "garbage"])
def test_invalid_balance_controls_cannot_become_zero(source):
    with pytest.raises(HTTPException):
        upload._ledger_decimal(source, balance=True)


def test_control_rows_extracted_before_exclusions_and_transactions_keep_original_values():
    rows = [row("", "", "<<< Balance Forward >>>", "", "", "100 DR"),
            row(), row("V2", text="Closing balance", credit=20, balance="70 DR"),
            row("", "", "Closing Balance:", 0, 30, "70 DR")]
    original = copy.deepcopy(rows)
    with ledger() as context:
        result = upload._prepare_ledger_rows(rows, request(excluded_rows=[2]))
        assert result == original[1:3]
        assert {k: context['evidence'][k] for k in ('opening', 'closing', 'control_rows')} == {"opening": "100", "closing": "70", "control_rows": [
            {"row_number": 2, "kind": "opening"}, {"row_number": 5, "kind": "closing"}]}
        assert upload._ledger_row_number(result[0], 2) == 3
        assert upload.filter_excluded_rows(result, [4]) == [original[1]]
        assert rows == original


def test_missing_controls_stay_missing_and_zero_controls_are_not_discarded():
    with ledger() as context:
        upload._prepare_ledger_rows([row()], request())
        assert context["evidence"]["opening"] is None
        assert context["evidence"]["closing"] is None
    with ledger() as context:
        upload._prepare_ledger_rows([row("", "", "Opening Balance", "", "", "0 DR")], request())
        assert context["evidence"]["opening"] == "0"


@pytest.mark.parametrize('text,expected', [('Dr 1,200.00','1200.00'), ('Cr 1,200.00','-1200.00'),
                                         ('(1200.00)','-1200.00'), ('(1200.00) Cr','-1200.00'), ('0 Dr','0')])
def test_erp_balance_presentations_are_numeric_only_in_calculations(text, expected):
    assert upload._ledger_decimal(text, balance=True) == Decimal(expected)
    with pytest.raises(HTTPException):
        upload._ledger_decimal(text)


def test_opening_is_inferred_and_all_original_balances_remain_unchanged():
    rows = [row(debit=50, credit=0, balance='200 Dr'), row('V2', day='06-01-2026', credit=25, balance='175 Dr')]
    original = copy.deepcopy(rows)
    with ledger() as context:
        upload._prepare_ledger_rows(rows, request(comparison_mode='replace_range'))
        check = context['evidence']['balance_check']
        assert check['opening'] == '150' and check['closing'] == '175'
        assert check['status'] == 'consistent' and check['opening_source'] == 'inferred_from_transaction'
        assert check['checked_balances'] == 2 and rows == original


@pytest.mark.parametrize('balance,debit,credit,expected', [('50 DR',50,0,'0'), ('200 CR',0,50,'-150')])
def test_zero_and_credit_opening_inference(balance, debit, credit, expected):
    with ledger() as context:
        upload._prepare_ledger_rows([row(debit=debit, credit=credit, balance=balance)], request(comparison_mode='replace_range'))
        assert context['evidence']['balance_check']['opening'] == expected


def test_missing_opening_is_never_assumed_zero_and_partial_extracts_cannot_prove_coverage():
    for req in [request(comparison_mode='replace_range'), request(comparison_mode='correction')]:
        with ledger() as context:
            upload._prepare_ledger_rows([row(balance='')], req)
            assert context['evidence']['balance_check']['opening'] is None
            assert context['evidence']['balance_check']['closing'] is None


def test_balance_discrepancies_and_reverse_or_unordered_dates_are_explicit():
    with ledger() as context:
        upload._prepare_ledger_rows([row('', '', 'Opening Balance', '', '', '100 DR'),
                                    row(balance='80 DR'), row('', '', 'Closing Balance', '', '', '70 DR')], request(comparison_mode='replace_range'))
        check = context['evidence']['balance_check']
        assert check['opening_source'] == 'erp_opening' and check['closing'] == '90'
        assert check['difference_count'] == 2 and check['differences'][0]['difference'] == '10'
    with ledger() as context:
        upload._prepare_ledger_rows([row('V2', day='06-01-2026', balance='80 DR'), row(balance='90 DR')], request(comparison_mode='replace_range'))
        check = context['evidence']['balance_check']
        assert check['order'] == 'reverse_source' and check['opening'] == '100' and check['closing'] == '80'
    with ledger() as context:
        upload._prepare_ledger_rows([row(day='05-01-2026'),row(day='07-01-2026'),row(day='06-01-2026')], request(comparison_mode='replace_range'))
        assert context['evidence']['balance_check']['status'] == 'unordered'


@pytest.mark.parametrize('condition', ['valid', 'gap', 'stale', 'changed', 'partial', 'legacy', 'missing_hash'])
def test_previous_closing_requires_contiguous_current_complete_evidence(condition):
    req = request(comparison_mode='replace_range')
    prior = {'id': 10, 'ledger_period_start':'2025-12-01', 'ledger_period_end':'2025-12-31',
             'ledger_evidence':{'balance_check':{'status':'consistent', 'opening':'100', 'opening_source':'erp_opening',
                'configuration':resolve()['configuration'], 'closing':'150', 'source_mode':'replace_range', 'canonical_fingerprint':'proof'}}}
    if condition == 'gap': prior['ledger_period_end'] = '2025-12-30'
    if condition == 'stale': prior['ledger_evidence']['continuity_stale'] = True
    if condition == 'partial': prior['ledger_evidence']['balance_check']['source_mode'] = 'correction'
    if condition == 'legacy': prior['ledger_evidence'] = {}
    with ledger() as context:
        upload._prepare_ledger_rows([row(balance='', debit=50, credit=0)], req)
        with patch.object(upload, 'execute_query_pg', return_value=[prior]) as query, \
             patch.object(upload, '_ledger_period_fingerprint', return_value='new' if condition == 'changed' else None if condition == 'missing_hash' else 'proof'):
            upload._connect_ledger_balances(req)
        check = context['evidence']['balance_check']
        assert query.call_args.args[1] == (3,7,'A-1','2026-01-01',20)
        if condition == 'valid':
            assert check['opening'] == '150' and check['closing'] == '200'
            assert check['opening_source'] == 'previous_coverage' and check['continuity']['status'] == 'matched'
        else:
            assert check['opening'] is None and check['status'] == 'opening_unavailable'


def test_new_source_opening_is_retained_and_compared_with_prior_closing():
    with ledger() as context:
        upload._prepare_ledger_rows([row(debit=50, credit=0, balance='200 DR')], request(comparison_mode='replace_range'))
        upload._calculate_ledger_balances(context, {'status':'available','expected_opening':'140','previous_upload_id':10})
        check = context['evidence']['balance_check']
        assert check['opening'] == '150' and check['continuity']['difference'] == '10'
        assert check['status'] == 'differences'


def test_earlier_import_invalidates_only_same_account_later_proofs_and_seals_new_canonical_hash():
    with ledger() as context:
        upload._prepare_ledger_rows([row()], request(comparison_mode='replace_range'))
        with patch.object(upload, 'execute_write_pg') as write, patch.object(upload, '_ledger_period_fingerprint', return_value='fresh'):
            upload._finalize_ledger_balances(request(comparison_mode='replace_range'), context['evidence'])
        sql, params = write.call_args_list[0].args
        assert 'ledger_period_end >= %s' in sql and 'id <> %s' in sql and 'continuity_stale' in sql
        assert params == (3,7,'A-1',20,'2026-01-01')
        stored = json.loads(write.call_args.args[1][0])
        assert stored['balance_check']['canonical_fingerprint'] == 'fresh'
        assert stored['balance_check']['source_mode'] == 'replace_range'
        assert 'balance_rows' not in stored
        assert 'canonical_fingerprint' not in context['evidence']['balance_check']


def test_concurrent_prior_ledger_change_cannot_seal_a_new_plan_with_old_opening():
    with ledger() as context, patch.object(upload, '_ledger_scope_fingerprint', side_effect=['old','new']), \
         patch.object(upload, '_connect_ledger_balances'), pytest.raises(HTTPException) as error:
        upload._prepare_ledger_balance_proof(request())
    assert error.value.status_code == 409 and 'changed while checking balances' in error.value.detail
    assert 'scope_fingerprint' not in context


@pytest.mark.parametrize('kind,expected', [('current',False),('stale',True),('legacy',True),('changed_hash',True)])
def test_unchanged_ledger_only_refreshes_evidence_when_its_prior_proof_requires_it(kind, expected):
    req = request(comparison_mode='replace_range')
    prior = {'id':10, 'ledger_evidence':{'balance_check':{'canonical_fingerprint':'current'}}}
    if kind == 'stale': prior['ledger_evidence']['continuity_stale'] = True
    if kind == 'legacy': prior['ledger_evidence'] = {}
    with ledger() as context:
        if kind != 'legacy':
            prior['ledger_evidence']['balance_check'].update(configuration=context['configuration'], opening_source='erp_opening')
        upload._prepare_ledger_rows([row()], req)
        with patch.object(upload, '_ledger_period_fingerprint', return_value='changed' if kind == 'changed_hash' else 'current'):
            assert upload._ledger_evidence_refresh_required(req, prior) is expected


def test_stale_balance_proof_refresh_has_no_new_changed_or_deleted_transaction_actions():
    req = request(comparison_mode='replace_range')
    with ledger() as context:
        source = row()
        upload._prepare_ledger_rows([source], req)
        context['scope_fingerprint'] = 'scope'
        existing = [{'id':101, 'version':1, 'row_data':source, 'extract_date':'2026-01-05',
                     'source_row_number':2, 'source_order_upload_id':10,
                     'source_identity':upload._canonical_source_identity(COLUMNS, source, 7)}]
        with patch.object(upload, 'execute_query_pg', return_value=existing):
            changes = upload.compare_transactions(3,7,COLUMNS,[source],{c['column_name']:c['column_name'] for c in COLUMNS},{},
                                                  comparison_mode='replace_range',date_delimiter='-',date_order='DMY')
        assert changes['unchanged_count'] == 1
        prior = {'id':10, 'ledger_evidence':{'continuity_stale':True}}
        assert upload._is_duplicate_upload_noop(prior, changes)
        assert upload._ledger_evidence_refresh_required(req, prior)
        payload = upload._build_validation_plan_payload(changes,COLUMNS,7,1,[],0,None,'-','DMY')
        assert payload['new'] == payload['changed'] == payload['missing'] == []
        with patch.object(upload, 'execute_write_pg') as write, patch.object(upload, '_ledger_period_fingerprint', return_value='sealed'):
            upload._finalize_ledger_balances(req, context['evidence'])
        assert all('UPDATE uploaded_files' in call.args[0] for call in write.call_args_list)


@pytest.mark.parametrize('condition', ['current', 'ancestor_changed', 'root_deleted', 'root_stale', 'roles_changed'])
def test_carried_balances_recheck_ancestor_rows_without_a_query_per_old_upload(condition):
    req = request(comparison_mode='replace_range')
    with ledger() as context:
        check = dict(status='consistent', source_mode='replace_range', opening_source='previous_coverage',
                     configuration=context['configuration'], opening='150', closing='200', canonical_fingerprint='own',
                     proof_start='2025-11-01', root_upload_id=1, root_opening='100', history_fingerprint='history')
        prior = dict(id=10, ledger_period_start='2025-12-01', ledger_period_end='2025-12-31', ledger_evidence={'balance_check':check})
        root = dict(id=1, ledger_period_start='2025-11-01', ledger_evidence={'balance_check':dict(
            status='consistent',source_mode='replace_range',opening_source='erp_opening',opening='100',configuration=context['configuration'])})
        if condition == 'root_stale': root['ledger_evidence']['continuity_stale'] = True
        if condition == 'roles_changed': check['configuration'] = 'old-roles'
        upload._prepare_ledger_rows([row(balance='', debit=50, credit=0)], req)
        with patch.object(upload, 'execute_query_pg', side_effect=[[prior], [] if condition == 'root_deleted' else [root]]) as query, \
             patch.object(upload, '_ledger_period_fingerprint', side_effect=['own','changed' if condition == 'ancestor_changed' else 'history']):
            upload._connect_ledger_balances(req)
        result = context['evidence']['balance_check']
        assert query.call_count <= 2
        if condition == 'current':
            assert result['opening'] == '200' and result['closing'] == '250'
        else:
            assert result['opening'] is None and result['status'] == 'opening_unavailable'


def test_duplicate_controls_and_outside_coverage_transactions_are_blocked():
    with ledger(), pytest.raises(HTTPException):
        upload._prepare_ledger_rows([row("", "", "Opening balance"), row("", "", "Opening balance")], request())
    with ledger(), pytest.raises(HTTPException):
        upload._prepare_ledger_rows([row(day="01-02-2026")], request())


def test_one_blank_movement_side_is_valid_but_both_blank_are_rejected():
    mapping = {item.template_column_id: item for item in request().mappings}
    with ledger():
        critical = upload.get_critical_field_columns(COLUMNS)
        assert upload.find_critical_blank_reasons(row(debit="", credit=10), critical, mapping) == []
        errors = upload.find_critical_blank_reasons(row(debit="", credit=""), critical, mapping)
        assert [item["role"] for item in errors] == ["amount"]


def test_account_identity_changes_without_repeating_account_in_source_row():
    identities = []
    for code in ("A-1", "B-2"):
        context = resolve(request(ledger_account_code=code))
        with ledger(context):
            identities.append(upload._canonical_source_identity(COLUMNS, row(), 7))
            assert "ledger:" + code in upload.get_document_group_id(COLUMNS, row(), 7)
    assert identities[0] != identities[1]
    assert "Account Code" not in row()


def test_compare_uses_account_and_declared_period_and_preserves_repeated_lines():
    with ledger(), patch.object(upload, "execute_query_pg", return_value=[]) as query:
        result = upload.compare_transactions(3, 7, COLUMNS, [row(), row()],
                                             {c["column_name"]: c["column_name"] for c in COLUMNS}, {},
                                             comparison_mode="replace_range", date_delimiter="-")
    sql, params = query.call_args.args
    assert "lu.ledger_account_code = %s" in sql and "cd.extract_date BETWEEN" in sql
    assert params == (3, 7, "A-1", "2026-01-01", "2026-01-31")
    assert result["new_count"] == 2 and len(set(result["new_source_identities"])) == 2
    assert result["date_range"] == {"start": "2026-01-01", "end": "2026-01-31"}


def test_no_activity_replacement_still_uses_declared_coverage():
    with ledger(), patch.object(upload, "execute_query_pg", return_value=[]):
        result = upload.compare_transactions(3, 7, COLUMNS, [], {}, {}, comparison_mode="replace_range")
    assert result["date_range"]["start"] == "2026-01-01"


def test_source_hash_lookup_cannot_cross_accounts_or_coverage():
    with ledger(), patch.object(upload, "execute_query_pg", return_value=[]) as query:
        upload._find_prior_import_with_same_hash(20, 3, 7, "hash")
    assert query.call_args.args[1] == (20, 3, 7, "hash", "A-1", "2026-01-01", "2026-01-31")


def test_validation_signature_binds_account_coverage_and_column_mapping():
    first = upload._validation_request_signature(request())
    assert first != upload._validation_request_signature(request(ledger_account_code="B-2"))
    assert first != upload._validation_request_signature(request(ledger_period_end="2026-01-30"))
    changed = request()
    changed.mappings[0].excel_column_name = "Different header"
    assert first != upload._validation_request_signature(changed)


def test_guard_rejects_changed_scope_before_metadata_writes():
    with ledger() as context:
        payload = {"ledger": {key: context[key] for key in ("account_code", "start", "end", "configuration")}}
        payload["ledger"]["scope_fingerprint"] = "old"
        with patch.object(upload, "_resolve_ledger_context", return_value=context), \
                patch.object(upload, "execute_query_pg", return_value=[TEMPLATE]), \
                patch.object(upload, "_ledger_scope_fingerprint", return_value="new"), \
                patch.object(upload, "execute_write_pg") as write, pytest.raises(HTTPException) as error:
            upload._guard_ledger_plan(request(), payload)
    assert error.value.status_code == 409
    write.assert_not_called()


def test_ledger_import_requires_validated_plan_and_context_is_reset():
    context = resolve()
    with patch.object(upload, "execute_query_pg", side_effect=[[TEMPLATE], [{"status": "Pending"}]]), \
            patch.object(upload, "_resolve_ledger_context", return_value=context), \
            patch.object(upload, "get_database_storage_budget_pg", return_value={"warning": False}), \
            pytest.raises(HTTPException):
        upload.validate_and_import(request())
    assert upload._ledger_context.get() is None


def test_balance_evidence_is_compact_and_not_transaction_payload():
    with ledger() as context, patch.object(upload, "execute_write_pg") as write:
        evidence = dict(opening="0", closing="-10", control_rows=[dict(kind="opening", row_number=2)])
        upload._persist_ledger_evidence(request(), evidence)
    params = write.call_args.args[1]
    assert params[:5] == (7, "A-1", "Cash A", "2026-01-01", "2026-01-31")
    assert json.loads(params[-2]) == evidence
    assert "Reference" not in params[-2]


@pytest.mark.parametrize("balance_only,date_kind", [(False, "text"), (False, "native"), (False, "serial"), (True, "text")])
@pytest.mark.parametrize('mode', ['correction', 'replace_range'])
def test_validate_and_import_extract_controls_and_never_classify_ledger_as_vouchers(balance_only, date_kind, mode, tmp_path):
    """Exercise both public handlers with in-memory input and captured writes."""
    from contextlib import ExitStack
    state = {"status": "Pending", "plan": None}
    captured = []
    locks = []
    template = {**TEMPLATE, "template_name": "Any client ledger", "rate_source": None}
    file = {"file_path": str(tmp_path / 'ledger.xlsx'), "file_name": "anything.xlsx", "upload_date": None,
            "uploaded_by": "Tester", "file_sha256": "hash", "client_id": 3, "template_id": 7}

    def query(sql, params=None, **kwargs):
        if sql.startswith('LOCK TABLE'):
            # Exercise the real DB helper: PostgreSQL lock commands have no
            # result description and fetching them raises ProgrammingError.
            from unittest.mock import MagicMock
            import psycopg2
            conn, cursor = MagicMock(), MagicMock()
            cursor.description = None
            cursor.fetchall.side_effect = psycopg2.ProgrammingError('no results to fetch')
            conn.cursor.return_value.__enter__.return_value = cursor
            token = database._pg_transaction.set(conn)
            try:
                result = database.execute_query_pg(sql, params)
                cursor.fetchall.assert_not_called()
                conn.commit.assert_not_called()
                locks.append(sql)
                return result
            finally:
                database._pg_transaction.reset(token)
        if 'FROM upload_lifecycle_runs' in sql:
            return [{'status':'archiving'}]
        if 'FROM upload_validation_plans' in sql:
            return [{'status':'ready','expires_at':None}]
        if 'AS fingerprint' in sql:
            return [{'fingerprint':'sealed-current-rows', 'incomplete':0}]
        if "FROM templates" in sql:
            return [template]
        if "FROM trial_balance_accounts a" in sql:
            return [dict(account_name="Cash A", classification="leaf", treasury_type="cash")]
        if "FROM template_column_mappings" in sql:
            return copy.deepcopy(COLUMNS)
        if "FROM clients" in sql:
            return [{"id": 3}]
        if "FROM uploaded_files" in sql and "WHERE id = %s" in sql:
            return [{**file, "status": state["status"]}]
        if "FROM client_data" in sql or "FROM uploaded_files" in sql or "pg_advisory" in sql:
            return []
        raise AssertionError("Unexpected SQL: " + sql)

    def write(sql, params=None):
        captured.append((sql, params))
        if "SET status = 'Imported'" in sql:
            state["status"] = "Imported"

    def save_plan(req, path, payload):
        # PostgreSQL JSONB removes native Python date types, just as real plans do.
        state["plan"] = json.loads(json.dumps(payload, default=str))
        return "plan-1"

    def summary(req, changes, count, status=None):
        if status:
            state["status"] = status

    workbook_rows = [row("", "", "Balance forward", "", "", "100 DR")]
    if not balance_only:
        day = (datetime(2026, 1, 5) if date_kind == "native" else
               (datetime(2026, 1, 5) - datetime(1899, 12, 30)).days if date_kind == "serial" else "05-01-2026")
        workbook_rows.append(row(day=day))
        workbook_rows.extend([row('', pd.NaT, text, None, None, None)
                              for text in ('Chq #: 123 Dt: 05-01-2026', 'Bill #: GRN-1')])
    closing = "100" if balance_only else "90"
    workbook_rows.append(row("", "", "Closing Balance", 0, 10, closing + " DR"))
    book = openpyxl.Workbook()
    book.active.title = 'Ledger'
    book.active.append([c['column_name'] for c in COLUMNS])
    for source in workbook_rows:
        book.active.append([None if pd.isna(value) else value for value in source.values()])
    book.save(file['file_path'])
    book.close()
    expected_rows = 0 if balance_only else 1
    with ExitStack() as stack:
        for name, value in [
            ("execute_query_pg", query), ("execute_write_pg", write),
            ("pg_transaction", lambda: nullcontext(object())),
            ("_create_validation_plan", save_plan),
            ("_load_validation_plan", lambda req, path: {"plan_json": state["plan"]}),
            ("_persist_upload_summary", summary),
            ("_ensure_uploaded_file_hash", lambda *args: "hash"),
            ("_check_upload_storage_budget", lambda *args: None),
            ("prepare_lifecycle", lambda *args: {'operation_id':'plan-1','ids':[], 'lifecycle':{}} if mode == 'replace_range' else None),
            ("get_database_storage_budget_pg", lambda *args: {"warning": False}),
            ("get_rate_precision", lambda: 0), ("log_audit", lambda *args: None),
            ("execute_batch_insert_with_ids_pg", lambda conn, sql, rows, **kwargs: captured.append((sql, rows)) or [101]),
            ("execute_batch_pg", lambda conn, sql, rows: captured.append((sql, rows))),
        ]:
            stack.enter_context(patch.object(upload, name, value))
        stack.enter_context(patch.object(upload.pd, "read_excel", return_value=pd.DataFrame(workbook_rows)))
        # Scope proofs and archive transport have separate lifecycle coverage;
        # retain its actual lock/result path in this public-handler regression.
        stack.enter_context(patch('services.upload_lifecycle.check_scope'))
        classification = stack.enter_context(patch.object(upload, "upsert_classification_rows_postgres"))
        treasury = stack.enter_context(patch.object(upload, "load_treasury_mapping_postgres"))
        occurrences = stack.enter_context(patch.object(upload, "resolve_upload_occurrences_postgres"))
        preparation = stack.enter_context(patch.object(upload, "_prepare_cashflow_snapshot_background"))
        invalidation = stack.enter_context(patch.object(upload, "invalidate_cashflow_summary_read_models"))
        options = dict(comparison_mode=mode, coverage_start='2026-01-01', coverage_end='2026-01-31',
                       confirm_empty_period=balance_only and mode == 'replace_range')
        validation = upload.validate_only(request(**options))
        assert validation["can_import"] and validation["new_rows_count"] == expected_rows
        assert validation["ledger_evidence"]["opening"] == "100"
        assert len(state["plan"]["new"]) == expected_rows
        if not balance_only:
            assert state['plan']['new'][0]['row_data'][upload.LEDGER_SOURCE_NOTES] == 'Chq #: 123 Dt: 05-01-2026\nBill #: GRN-1'
            assert len(validation['ledger_evidence']['continuation_rows']) == 2
        if mode == 'replace_range':
            assert validation['ledger_evidence']['balance_check']['opening'] == '100'
            assert validation['ledger_evidence']['balance_check']['closing'] == closing
        result = upload.validate_and_import(request(validation_plan_id="plan-1", **options))
        assert result["imported_count"] == expected_rows and result["deleted_count"] == 0
        for call in (classification, treasury, occurrences, preparation, invalidation):
            call.assert_not_called()
        assert len(locks) == (2 if mode == 'replace_range' else 0)
    controls = [params for sql, params in captured if "ledger_opening_balance = %s" in sql]
    assert len(controls) == 1 and controls[0][5:7] == ("100", closing)
    sealed = [json.loads(params[0]) for sql, params in captured if sql.startswith('UPDATE uploaded_files SET ledger_evidence = %s')]
    assert sealed[0]['balance_check']['canonical_fingerprint'] == 'sealed-current-rows'
    if not balance_only:
        inserted = next(params for sql, params in captured if 'INSERT INTO client_data' in sql)
        assert inserted[0][3].adapted[upload.LEDGER_SOURCE_NOTES] == 'Chq #: 123 Dt: 05-01-2026\nBill #: GRN-1'
    assert upload._ledger_context.get() is None


def test_invalid_transaction_date_is_a_clear_validation_error():
    with ledger(), pytest.raises(HTTPException) as error:
        upload._prepare_ledger_rows([row(day="not a date")], request())
    assert error.value.status_code == 400 and "row 2" in error.value.detail


def test_ledger_amount_extraction_uses_configured_roles_and_keeps_zero():
    with ledger():
        assert upload.extract_columns_from_row(row(debit="", credit="1,234.56"), COLUMNS, "-", "DMY") == (-1234.56, "2026-01-05")
        assert upload.extract_columns_from_row(row(debit=0, credit=0), COLUMNS, "-", "DMY") == (0, "2026-01-05")


def test_setup_backup_restore_preserves_ledger_context_and_row_identity_without_live_connection():
    from unittest.mock import MagicMock
    conn = MagicMock()
    payload = templates.BackupData(
        columns=[], mappings=[], clients=[],
        templates=[dict(id=7, template_name="Ledger", data_purpose="account_ledger", ledger_columns=ROLES)],
        uploaded_files=[dict(id=20, file_name="ledger.xlsx", file_path="external/ledger.xlsx", client_id=3,
                             template_id=7, ledger_account_code="A-1", ledger_account_name="Cash A",
                             ledger_period_start="2026-01-01", ledger_period_end="2026-01-31",
                             ledger_opening_balance="100", ledger_closing_balance="90", ledger_evidence="{}")],
        client_data=[dict(id=101, client_id=3, template_id=7, upload_id=20, row_data=row(),
                         source_identity="account-line", source_fingerprint="fingerprint", document_group_id="7-ledger:A-1:V1")],
    )
    with patch.object(templates, "get_raw_conn_pg", return_value=conn), \
            patch.object(templates, "release_raw_conn_pg"), patch.object(templates, "log_audit"):
        templates.import_backup(payload)
    statements = conn.cursor.return_value.execute.call_args_list
    for call in statements:
        if len(call.args) > 1:
            assert call.args[0].count("%s") == len(call.args[1])
    restored_template = next(call.args[1] for call in statements if "INSERT INTO templates" in call.args[0])
    assert restored_template[-2] == "account_ledger" and json.loads(restored_template[-1]) == ROLES
    restored_upload = next(call.args[1] for call in statements if "INSERT INTO uploaded_files" in call.args[0])
    assert restored_upload[8:10] == (7, "A-1")
    restored_row = next(call.args[1] for call in statements if "INSERT INTO client_data" in call.args[0])
    assert restored_row[-3:] == ("account-line", "fingerprint", "7-ledger:A-1:V1")


class LedgerSetupStore:
    """Transactional in-memory protocol; never connects to the user's database."""
    def __init__(self, existing=5):
        self.columns = [dict(id=i + 1, column_name=name, data_type=kind, is_active=True)
                        for i, (name, kind) in enumerate(templates.LEDGER_SETUP_COLUMNS.values())][:existing]
        self.templates, self.mappings, self.writes = [], [], []
        self.fail_template = False
    def read(self, sql, params=None):
        if 'FROM master_columns ORDER BY' in sql: return copy.deepcopy(self.columns)
        if 'FROM master_columns WHERE id = ANY' in sql:
            return [c for c in self.columns if c['id'] in params[0] and c['is_active']]
        if 'FROM templates' in sql:
            return [t for t in self.templates if t['template_name'].strip().lower() == params[0].strip().lower()]
        raise AssertionError(sql)
    def write(self, sql, params=None):
        self.writes.append((sql, params))
        if sql.startswith('LOCK TABLE'): return
        if 'INSERT INTO master_columns' in sql:
            new_id = len(self.columns) + 1
            self.columns.append(dict(id=new_id, column_name=params[0], data_type=params[1], is_active=True))
            return new_id
        if 'INSERT INTO templates' in sql:
            if self.fail_template: raise RuntimeError('simulated save failure')
            new_id = len(self.templates) + 10
            self.templates.append(dict(id=new_id, template_name=params[0], data_purpose=params[4], ledger_columns=params[5], is_active=True, has_subtotals_by_default=params[3]))
            return new_id
        if 'INSERT INTO template_column_mappings' in sql: self.mappings.append(params); return
        raise AssertionError(sql)
    @contextmanager
    def transaction(self):
        before = copy.deepcopy((self.columns, self.templates, self.mappings))
        try: yield object()
        except BaseException:
            self.columns, self.templates, self.mappings = before
            raise
    @contextmanager
    def patched(self):
        with patch.object(templates, 'execute_query_pg', side_effect=self.read), \
             patch.object(templates, 'execute_write_pg', side_effect=self.write), \
             patch.object(templates, 'pg_transaction', side_effect=self.transaction), \
             patch.object(templates, 'log_audit'), patch.object(templates, 'invalidate_template_cache'):
            yield self


def test_ledger_preparation_preview_is_read_only_and_save_reuses_columns_idempotently():
    with LedgerSetupStore().patched() as store:
        preview = templates.preview_ledger_setup()
        assert [c['column_id'] for c in preview['columns']] == [1, 2, 3, 4, 5, None]
        assert store.writes == []
        setup = templates.LedgerSetupRequest(ledger_columns={role: None for role in templates.LEDGER_SETUP_COLUMNS})
        first = templates.save_ledger_setup(setup)
        second = templates.save_ledger_setup(setup)
        assert first['template_id'] == second['template_id'] == 10 and second['reused']
        assert len(store.columns) == 6 and len(store.templates) == 1 and len(store.mappings) == 6
        assert store.writes[0][0].startswith('LOCK TABLE')
        assert [c['column_name'] for c in store.columns][:5] == [v[0] for v in list(templates.LEDGER_SETUP_COLUMNS.values())[:5]]


def test_failed_template_save_rolls_back_all_missing_columns():
    with LedgerSetupStore(existing=0).patched() as store:
        store.fail_template = True
        with pytest.raises(RuntimeError, match='simulated save failure'):
            templates.save_ledger_setup(templates.LedgerSetupRequest(ledger_columns={r: None for r in templates.LEDGER_SETUP_COLUMNS}))
        assert store.columns == store.templates == store.mappings == []


@pytest.mark.parametrize('problem', ['wrong_type', 'inactive', 'ambiguous'])
def test_ledger_preparation_never_changes_incompatible_shared_columns(problem):
    with LedgerSetupStore().patched() as store:
        if problem == 'wrong_type': store.columns[0]['data_type'] = 'Date'
        elif problem == 'inactive': store.columns[0]['is_active'] = False
        else: store.columns.append({**store.columns[0], 'id': 8})
        before = copy.deepcopy(store.columns)
        with pytest.raises(HTTPException) as error: templates.preview_ledger_setup()
        assert error.value.status_code == 409
        assert store.columns == before and store.writes == []


def test_ledger_setup_rejects_duplicate_roles_and_existing_template_reassignment():
    with LedgerSetupStore(existing=6).patched() as store:
        roles = {role: i + 1 for i, role in enumerate(templates.LEDGER_SETUP_COLUMNS)}
        for invalid in [{**roles, 'credit': 4}, {**roles, 'balance': 999}]:
            with pytest.raises(HTTPException):
                templates.save_ledger_setup(templates.LedgerSetupRequest(ledger_columns=invalid))
        store.templates = [dict(id=10, template_name='Account Ledger', data_purpose='standard', ledger_columns='{}', is_active=True)]
        with pytest.raises(HTTPException) as error:
            templates.save_ledger_setup(templates.LedgerSetupRequest(ledger_columns=roles))
        assert error.value.status_code == 409 and store.templates[0]['data_purpose'] == 'standard'
        assert store.mappings == []


def test_account_picker_and_validation_share_all_latest_trial_balance_posting_accounts():
    account = {'account_code': 'A-1', 'account_name': '<Supplier A>', 'classification': 'leaf'}
    with patch.object(upload, 'execute_query_pg', side_effect=[[{'id': 3}], [account]]) as query:
        result = upload.get_ledger_upload_accounts(3)
    assert result == {'accounts': [{'account_code': 'A-1', 'account_name': '<Supplier A>'}]}
    sql, params = query.call_args.args
    assert params == (3,) and "status = 'parsed'" in sql and "classification = 'leaf'" in sql
    assert 'treasury' not in sql and 'ORDER BY (period_end IS NULL), period_end DESC, uploaded_at DESC' in sql
    with patch.object(upload, 'execute_query_pg', side_effect=[[{'id': 3}], [account, account]]), pytest.raises(HTTPException):
        upload.get_ledger_upload_accounts(3)
    with patch.object(upload, 'execute_query_pg', return_value=[]), pytest.raises(HTTPException):
        upload.get_ledger_upload_accounts(99)


def test_ledger_setup_routes_resolve_before_template_id_and_save_as_one_operation():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    app=FastAPI(); app.include_router(templates.router)
    with LedgerSetupStore().patched() as store, TestClient(app) as client:
        preview=client.get('/api/templates/ledger-setup')
        assert preview.status_code==200 and len(preview.json()['columns'])==6
        response=client.post('/api/templates/ledger-setup',json={
            'template_name':'Account Ledger','ledger_columns':{role:None for role in templates.LEDGER_SETUP_COLUMNS}})
        assert response.status_code==200 and response.json()['template_id']==10
        assert len(store.templates)==1 and len(store.columns)==6


def test_prepared_ledger_keeps_the_requested_subtotal_setting():
    with LedgerSetupStore(existing=6).patched() as store:
        templates.save_ledger_setup(templates.LedgerSetupRequest(ledger_columns={role:None for role in templates.LEDGER_SETUP_COLUMNS},has_subtotals_by_default=True))
        assert store.templates[0]['has_subtotals_by_default'] is True


@pytest.mark.parametrize('value', [None, pd.NaT, pd.NA, float('nan'), '', '   '])
def test_missing_excel_date_sentinels_are_empty_before_native_datetime_detection(value):
    assert upload.classify_date_cell(value) == 'empty'


def prepare_note_source(source, req=None):
    """Use the real projection/positions boundary, including unmapped cells."""
    token = upload._source_row_numbers.set({})
    try:
        prepared, diagnostics = upload._prepare_source_rows(pd.DataFrame(source), req or request(), COLUMNS,
            {c['column_name']: c['column_name'] for c in COLUMNS})
        return upload._prepare_ledger_rows(prepared, req or request()), diagnostics
    finally:
        upload._source_row_numbers.reset(token)


def test_continuation_notes_keep_original_values_positions_and_financial_line_multiplicity():
    source = [row(), row('', pd.NaT, '  Cheque <A>  ', None, None, None),
              row('', pd.NaT, 'Bill #: 12\nsource detail', None, None, None), row()]
    original = copy.deepcopy(source)
    with ledger() as context:
        result, diagnostics = prepare_note_source(source, request(comparison_mode='replace_range'))
        assert len(result) == 2 and diagnostics['excluded_count'] == 0
        assert context['evidence']['continuation_rows'] == [
            {'row_number':3, 'parent_row_number':2, 'voucher':'V1'},
            {'row_number':4, 'parent_row_number':2, 'voucher':'V1'}]
        assert len(context['balance_rows']) == 2
        mapping = {c['column_name']:c['column_name'] for c in COLUMNS}
        canonical = upload._canonical_import_row_data(result[0], mapping, COLUMNS, None, 0)
        assert canonical['Narrative'] == 'Payment' and canonical['Money out'] == 10
        assert canonical['Book balance'] == '90 DR'
        assert canonical[upload.LEDGER_SOURCE_NOTES] == '  Cheque <A>  \nBill #: 12\nsource detail'
        assert upload._ledger_row_number(result[1], 0) == 5
        assert list(canonical)[0] == 'Reference'
    assert source == original


@pytest.mark.parametrize('case', ['orphan', 'blank_gap', 'control_boundary', 'parent_excluded', 'note_excluded', 'invalid_parent'])
def test_ambiguous_or_excluded_continuation_cannot_be_silently_attached(case):
    note = row('', pd.NaT, 'Bill #: source', None, None, None)
    source, excluded = [row(), note], []
    if case == 'orphan': source = [note]
    if case == 'blank_gap': source.insert(1, {key:None for key in row()})
    if case == 'control_boundary': source.insert(1, row('', None, 'Closing Balance', None, None, '90 DR'))
    if case == 'parent_excluded': excluded = [2]
    if case == 'note_excluded': excluded = [3]
    if case == 'invalid_parent': source[0]['Posting day'] = pd.NaT
    with ledger(), pytest.raises(HTTPException, match='Ledger note'):
        prepare_note_source(source, request(excluded_rows=excluded))


def test_unmapped_source_content_prevents_note_recognition_and_real_missing_dates_stay_invalid():
    with ledger() as context:
        source = [row(), {**row('', pd.NaT, 'Bill #: 1', None, None, None), 'Extra reference':'UNKNOWN'}]
        result, _ = prepare_note_source(source)
        assert len(result) == 2 and context['evidence']['continuation_rows'] == []
        assert context['balance_incomplete']
        result, _ = prepare_note_source([row(day=pd.NaT)])
        assert len(result) == 1 and context['balance_incomplete']
        reasons = upload.find_critical_blank_reasons(result[0], upload.get_critical_field_columns(COLUMNS),
            {m.template_column_id:m for m in request().mappings})
        assert any(reason['role'] == 'date' for reason in reasons)


def test_reserved_source_note_column_cannot_overwrite_client_template_values():
    columns = COLUMNS + [{'id':99, 'column_name':upload.LEDGER_SOURCE_NOTES, 'data_type':'Text'}]
    with patch.object(upload, 'execute_query_pg', side_effect=[[dict(account_name='Cash A')], columns]), \
            pytest.raises(HTTPException, match='reserved'):
        upload._resolve_ledger_context(request(), TEMPLATE)


def test_coverage_error_identifies_genuine_prior_year_voucher_and_does_not_drop_it():
    source = row('JV-1225-100', datetime(2025, 12, 31), 'Opening Balance Adjustment', 984286, 0, '984,286 DR')
    with ledger(), pytest.raises(HTTPException) as error:
        prepare_note_source([source])
    assert all(text in error.value.detail for text in ('Row 2', 'JV-1225-100', '2025-12-31', '2026-01-01', '2026-01-31'))
    req = request(ledger_period_start='2025-12-31', comparison_mode='replace_range')
    with ledger(resolve(req)) as context:
        result, _ = prepare_note_source([source], req)
        assert len(result) == 1
        assert context['evidence']['balance_check']['opening'] == '0'


@pytest.mark.parametrize('new_note', ['original note', 'revised note', None])
def test_reupload_notes_are_compared_archived_and_included_in_complete_voucher_report(new_note):
    from io import BytesIO
    from openpyxl import load_workbook
    from services.upload_observation_report import build_observation_report
    req = request(comparison_mode='replace_range')
    mapping = {c['column_name']:c['column_name'] for c in COLUMNS}
    with ledger() as context:
        context['scope_fingerprint'] = 'isolated-source-proof'
        first, _ = prepare_note_source([row(), row('', pd.NaT, 'original note', None, None, None)], req)
        old = upload._canonical_import_row_data(first[0], mapping, COLUMNS, None, 0)
        # Preserve two legitimate identical lines, with occurrence-specific identities.
        old_rows = [dict(id=index, row_data=copy.deepcopy(old), version=1, extract_date='2026-01-05',
            document_group_id=upload.get_document_group_id(COLUMNS, old, 7),
            source_identity=upload._canonical_source_identity(COLUMNS, old, 7, occurrence_index=index)) for index in (1, 2)]
        source = []
        for _ in range(2):
            source.append(row(debit=0.0, credit=10.0))
            if new_note is not None: source.append(row('', pd.NaT, new_note, None, None, None))
        incoming, _ = prepare_note_source(source, req)
        def query(sql, params=None, **kwargs):
            return old_rows if 'AS scope_row_hash' in sql or 'SELECT cd.id FROM client_data' in sql else []
        with patch.object(upload, 'execute_query_pg', side_effect=query):
            changes = upload.compare_transactions(3, 7, COLUMNS, incoming, mapping, {},
                comparison_mode='replace_range', date_delimiter='-', date_order='DMY')
        if new_note == 'original note':
            assert changes['unchanged_count'] == 2
            assert changes['new_count'] == changes['missing_count'] == 0
        else:
            assert changes['new_count'] == changes['missing_count'] == 2
            assert len(set(changes['new_source_identities'])) == 2
            observation = changes['voucher_observations'][0]
            assert observation['changed_fields'] == [upload.LEDGER_SOURCE_NOTES]
            payload = upload._build_validation_plan_payload(changes, COLUMNS, 7, 2, [], 0, None, '-', 'DMY')
            assert payload['template_fingerprint'] == upload.digest([COLUMNS, None])
            assert payload['template_columns'][-1]['column_name'] == upload.LEDGER_SOURCE_NOTES
            assert upload.LEDGER_SOURCE_NOTES not in json.dumps(context['evidence'])
            scope = {'client_id':3, 'template_id':7, 'start':'2026-01-01', 'end':'2026-01-31'}
            data = build_observation_report(changes['voucher_observations'], old_rows, payload['template_columns'], scope)
            assert data == build_observation_report(changes['voucher_observations'], old_rows, payload['template_columns'], scope)
            workbook = load_workbook(BytesIO(data))
            assert workbook['Previous vouchers'].cell(6, 10).value == 'original note'
            assert workbook['Incoming vouchers'].cell(6, 10).value == new_note


def test_ordinary_template_does_not_inherit_ledger_note_recognition():
    source = [row(), row('', pd.NaT, 'free text', None, None, None)]
    prepared, _ = upload._prepare_source_rows(pd.DataFrame(source), request(), COLUMNS,
        {c['column_name']:c['column_name'] for c in COLUMNS})
    assert len(upload._prepare_ledger_rows(prepared, request())) == 2


@pytest.fixture
def ledger_preview_client(monkeypatch):
    """Execute preview SQL on owned in-memory data, never application storage."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from routers import data
    conn = sqlite3.connect(':memory:', check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript('''
        CREATE TABLE clients (id INTEGER, is_active BOOLEAN);
        CREATE TABLE templates (id INTEGER, template_name TEXT, data_purpose TEXT, is_active BOOLEAN);
        CREATE TABLE uploaded_files (id INTEGER, client_id INTEGER, template_id INTEGER,
            status TEXT, ledger_account_code TEXT, ledger_account_name TEXT);
        CREATE TABLE client_data (id INTEGER, client_id INTEGER, template_id INTEGER, upload_id INTEGER,
            row_data TEXT, extract_amount REAL, extract_date TEXT, data_quality_flag TEXT, rate_source TEXT,
            document_group_id TEXT, is_deleted BOOLEAN, change_detection_status TEXT,
            source_row_number INTEGER, source_order_upload_id INTEGER);
        INSERT INTO clients VALUES (3, TRUE), (4, TRUE);
        INSERT INTO templates VALUES (7, 'Universal ledger', 'account_ledger', TRUE),
            (8, 'Invoices', 'standard', TRUE), (9, 'Another ledger', 'account_ledger', TRUE);
        INSERT INTO uploaded_files VALUES (20,3,7,'Imported','A-1','Old cash name'),
            (21,3,7,'Imported','B-2','Bank beta'), (22,4,7,'Imported','A-1','Other client cash'),
            (23,3,9,'Imported','C-3','Other template'), (24,3,7,'Pending','D-4','Not imported'),
            (25,3,7,'Imported','A-1','Renamed cash'), (26,3,7,'Imported',NULL,'Unassigned'),
            (27,3,7,'Imported','Z-0','Zero activity ledger'), (28,4,7,'Imported','X-ONLY','Other client only');
    ''')
    sources = [(101,3,7,20,100,'2026-01-04','Alpha',None,False,'active','ledger:A-1:V1'),
               (102,3,7,20,100,'2026-01-04','Alpha',None,False,'active','ledger:A-1:V1'),
               (103,3,7,21,200,'2026-01-04','Beta',None,False,'active','ledger:B-2:V1'),
               (104,4,7,22,999,'2026-01-04','Other client',None,False,'active','other-client'),
               (105,3,9,23,999,'2026-01-04','Other template',None,False,'active','other-template'),
               (106,3,7,24,999,'2026-01-04','Pending',None,False,'active','pending'),
               (107,3,7,20,50,'2026-01-12','Second detail','incomplete',False,'active','ledger:A-1:V2'),
               (108,3,7,20,999,'2026-01-04','Deleted',None,True,'active','deleted'),
               (109,3,7,20,999,'2026-01-04','Deleted status',None,False,'deleted','deleted-status'),
               (110,3,8,20,10,'2026-01-04','Ordinary',None,False,'active','ordinary')]
    for key, client, template, source, amount, day, text, quality, deleted, status, group in sources:
        raw = row('V1' if key != 107 else 'V2', day, text, amount, 0, str(amount)+' DR')
        conn.execute('INSERT INTO client_data VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (key,client,template,source,json.dumps(raw),amount,day,quality,None,group,deleted,status,None,None))
    calls = []
    def query(sql, params=None, **kwargs):
        calls.append((sql, params))
        adapted = sql.replace('%s', '?').replace('jsonb_each_text', 'json_each')
        adapted = adapted.replace('EXTRACT(DOW FROM cd.extract_date)', "CAST(strftime('%w', cd.extract_date) AS INTEGER)")
        results = [dict(r) for r in conn.execute(adapted, params or ())]
        for result in results:
            if 'row_data' in result: result['row_data'] = json.loads(result['row_data'])
        return results
    monkeypatch.setattr(data, 'execute_query_pg', query)
    monkeypatch.setattr(data, 'get_template_columns_ordered', lambda tid: COLUMNS)
    monkeypatch.setattr(data, 'attach_history_flags', lambda rows: None)
    monkeypatch.setattr(data, 'attach_assignment_flags', lambda rows: None)
    monkeypatch.setattr(data, 'execute_write_pg', lambda *a, **k: pytest.fail('Preview attempted a write'))
    app = FastAPI(); app.include_router(data.router)
    with TestClient(app) as client:
        yield client, conn, calls
    conn.close()


def test_ledger_picker_uses_latest_imported_names_and_keeps_client_template_scope(ledger_preview_client):
    client, conn, _ = ledger_preview_client
    changes = conn.total_changes
    response = client.get('/api/data/ledger-accounts', params={'client_id':3,'template_id':7})
    assert response.status_code == 200
    assert response.json()['accounts'] == [
        {'account_code':'B-2','account_name':'Bank beta'}, {'account_code':'A-1','account_name':'Renamed cash'},
        {'account_code':'Z-0','account_name':'Zero activity ledger'}]
    assert conn.total_changes == changes


@pytest.mark.parametrize('filters,expected_ids,expected_sundays', [
    ({}, [101,102,107], 2), ({'search':'Second'}, [107], 0),
    ({'amount_min':80,'amount_max':120}, [101,102], 2),
    ({'sunday_only':'true'}, [101,102], 2), ({'data_quality':'incomplete'}, [107], 0),
    ({'date_start':'2026-01-10'}, [107], 0), ({'date_end':'2026-01-03'}, [], 0),
])
def test_account_scope_applies_to_rows_counts_sundays_and_existing_filters(ledger_preview_client, filters, expected_ids, expected_sundays):
    client, conn, calls = ledger_preview_client
    changes = conn.total_changes
    params = {'client_id':3,'template_id':7,'date_start':'2026-01-01','date_end':'2026-01-31',
              'ledger_account_code':'A-1', **filters}
    response = client.get('/api/data/preview', params=params)
    assert response.status_code == 200, response.text
    result = response.json()
    assert [r['id'] for r in result['rows']] == expected_ids
    assert result['total_rows'] == len(expected_ids) and result['sunday_count'] == expected_sundays
    assert conn.total_changes == changes
    scoped_queries = [sql for sql, _ in calls if 'FROM client_data cd' in sql]
    assert scoped_queries and all('lu.ledger_account_code = %s' in sql for sql in scoped_queries)


def test_ledger_preview_keeps_repeated_lines_and_account_grouping_through_pages(ledger_preview_client):
    client, _, _ = ledger_preview_client
    params = dict(client_id=3,template_id=7,date_start='2026-01-01',date_end='2026-01-31',ledger_account_code='A-1',limit=1)
    pages = [client.get('/api/data/preview', params={**params,'page':page}).json() for page in (1,2,3)]
    assert [page['rows'][0]['id'] for page in pages] == [101,102,107]
    assert all(page['total_rows'] == 3 for page in pages)
    assert pages[0]['rows'][0]['document_group_id'] == pages[1]['rows'][0]['document_group_id'] == 'ledger:A-1:V1'
    other = client.get('/api/data/preview', params={**params,'ledger_account_code':'B-2'}).json()
    assert other['total_rows'] == 1 and other['rows'][0]['id'] == 103
    assert other['rows'][0]['document_group_id'] != pages[0]['rows'][0]['document_group_id']


def test_ledger_preview_uses_source_positions_before_pagination_without_changing_values(ledger_preview_client):
    client, conn, _ = ledger_preview_client
    # ID order is deliberately the opposite of this ERP's same-day row order.
    conn.execute('UPDATE client_data SET source_row_number=4,source_order_upload_id=90 WHERE id=101')
    conn.execute('UPDATE client_data SET source_row_number=2,source_order_upload_id=90 WHERE id=102')
    before = list(conn.execute('SELECT id,row_data,upload_id FROM client_data ORDER BY id'))
    params = dict(client_id=3, template_id=7, date_start='2026-01-01', date_end='2026-01-31',
                  ledger_account_code='A-1', limit=1)
    pages = [client.get('/api/data/preview', params={**params, 'page':p}).json() for p in (1,2,3)]
    assert [p['rows'][0]['id'] for p in pages] == [102,101,107]
    assert [p['rows'][0]['row_data']['Reference'] for p in pages] == ['V1','V1','V2']
    assert list(conn.execute('SELECT id,row_data,upload_id FROM client_data ORDER BY id')) == before


def test_comparison_keeps_original_source_rows_despite_voucher_sort_and_note_gaps():
    req = request(comparison_mode='replace_range')
    source = [row('V9', debit=100, credit=0, balance='100 DR'),
              row('', pd.NaT, 'Attached source note', None,None,None),
              row('V1', debit=0, credit=10, balance='90 DR'),
              row('V1', debit=0, credit=0, balance='90 DR')]
    token = upload._source_row_numbers.set({})
    try:
        with ledger() as context:
            prepared, _ = prepare_note_source(source, req)
            context['scope_fingerprint'] = 'same-account-only'
            with patch.object(upload, 'execute_query_pg', return_value=[]):
                changes = upload.compare_transactions(3,7,COLUMNS,prepared,
                    {c['column_name']:c['column_name'] for c in COLUMNS},{}, comparison_mode='replace_range',
                    date_delimiter='-', date_order='DMY')
            payload = upload._build_validation_plan_payload(changes,COLUMNS,7,3,[],0,None,'-','DMY')
            ordered = sorted(payload['new'], key=lambda r:r['source_row_number'])
            assert [r['row_data']['Reference'] for r in ordered] == ['V9','V1','V1']
            assert [r['source_row_number'] for r in ordered] == [2,4,5]
            assert ordered[0]['row_data'][upload.LEDGER_SOURCE_NOTES] == 'Attached source note'
            assert ordered[1]['row_data']['Book balance'] == ordered[2]['row_data']['Book balance'] == '90 DR'
            assert len({r['source_identity'] for r in ordered}) == 3
    finally:
        upload._source_row_numbers.reset(token)


@pytest.mark.parametrize('template,account', [(7,''),(7,'C-3'),(7,'X-ONLY'),(7,'D-4'),
    (7,"A-1' OR 1=1--"),(7,'x'*121),(8,'A-1')])
def test_invalid_or_foreign_ledger_account_is_rejected_without_fetching_rows(ledger_preview_client, template, account):
    client, _, calls = ledger_preview_client
    response = client.get('/api/data/preview', params=dict(client_id=3,template_id=template,
        date_start='2026-01-01',date_end='2026-01-31',ledger_account_code=account))
    assert response.status_code == 400
    assert not any('FROM client_data cd' in sql for sql, _ in calls)


def test_account_filter_remains_optional_for_existing_raw_consumers_and_standard_templates(ledger_preview_client):
    client, _, _ = ledger_preview_client
    response = client.get('/api/data/preview', params=dict(client_id=3,template_id=8,
        date_start='2026-01-01',date_end='2026-01-31'))
    assert response.status_code == 200 and response.json()['total_rows'] == 1
    assert client.get('/api/data/ledger-accounts',params={'client_id':3,'template_id':8}).status_code == 400
    assert client.get('/api/data/ledger-accounts',params={'client_id':999,'template_id':7}).status_code == 404
