"""Account ledger source controls; no real workbook upload or database access."""
import copy
import json
import sqlite3
from contextlib import contextmanager, nullcontext
from decimal import Decimal
from datetime import datetime
from unittest.mock import patch

import pandas as pd
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
        [account or dict(account_name="Cash A", classification="leaf", treasury_type="cash")], COLUMNS,
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


def test_account_selection_requires_current_trial_balance_and_cash_bank_configuration():
    with patch.object(upload, "execute_query_pg", return_value=[]) as query, pytest.raises(HTTPException):
        upload._resolve_ledger_context(request(), TEMPLATE)
    sql, params = query.call_args.args
    assert "classification = 'leaf'" in sql and "status = 'parsed'" in sql and "t.is_active = TRUE" in sql
    assert params == (3, "A-1", 3)


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
        assert context["evidence"] == {"opening": "100", "closing": "70", "control_rows": [
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
def test_validate_and_import_extract_controls_and_never_classify_ledger_as_vouchers(balance_only, date_kind):
    """Exercise both public handlers with in-memory input and captured writes."""
    from contextlib import ExitStack
    state = {"status": "Pending", "plan": None}
    captured = []
    template = {**TEMPLATE, "template_name": "Any client ledger", "rate_source": None}
    file = {"file_path": "unused.xlsx", "file_name": "anything.xlsx", "upload_date": None,
            "uploaded_by": "Tester", "file_sha256": "hash", "client_id": 3, "template_id": 7}

    def query(sql, params=None, **kwargs):
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
    closing = "100" if balance_only else "90"
    workbook_rows.append(row("", "", "Closing Balance", 0, 10, closing + " DR"))
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
            ("get_database_storage_budget_pg", lambda *args: {"warning": False}),
            ("get_rate_precision", lambda: 0), ("log_audit", lambda *args: None),
            ("execute_batch_insert_with_ids_pg", lambda *args, **kwargs: [101]),
            ("execute_batch_pg", lambda conn, sql, rows: captured.append((sql, rows))),
        ]:
            stack.enter_context(patch.object(upload, name, value))
        stack.enter_context(patch.object(upload.pd, "read_excel", return_value=pd.DataFrame(workbook_rows)))
        classification = stack.enter_context(patch.object(upload, "upsert_classification_rows_postgres"))
        treasury = stack.enter_context(patch.object(upload, "load_treasury_mapping_postgres"))
        occurrences = stack.enter_context(patch.object(upload, "resolve_upload_occurrences_postgres"))
        preparation = stack.enter_context(patch.object(upload, "_prepare_cashflow_snapshot_background"))
        invalidation = stack.enter_context(patch.object(upload, "invalidate_cashflow_summary_read_models"))
        validation = upload.validate_only(request())
        assert validation["can_import"] and validation["new_rows_count"] == expected_rows
        assert validation["ledger_evidence"]["opening"] == "100"
        assert len(state["plan"]["new"]) == expected_rows
        result = upload.validate_and_import(request(validation_plan_id="plan-1"))
        assert result["imported_count"] == expected_rows and result["deleted_count"] == 0
        for call in (classification, treasury, occurrences, preparation, invalidation):
            call.assert_not_called()
    controls = [params for sql, params in captured if "ledger_opening_balance = %s" in sql]
    assert len(controls) == 1 and controls[0][5:7] == ("100", closing)
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
