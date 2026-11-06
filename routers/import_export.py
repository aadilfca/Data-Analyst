from fastapi import APIRouter, BackgroundTasks, Depends, File, UploadFile, HTTPException, Body
from fastapi.responses import StreamingResponse
import hashlib, os, json, logging, openpyxl, pandas as pd
from io import BytesIO
from functools import wraps
from contextvars import ContextVar
from decimal import Decimal, InvalidOperation
import re
import math
import database
from time import perf_counter
from uuid import uuid4
from database import (
    execute_query_pg, execute_write_pg, log_audit, pg_transaction,
    log_data_audit_pg, execute_batch_pg, execute_batch_insert_with_ids_pg,
    start_db_metrics, get_db_metrics, get_db_query_details, stop_db_metrics, log_error,
    compact_audit_payloads, get_database_storage_budget_pg, resolve_document_column,
)
from psycopg2.extras import Json
from services.cash_bank_classification import load_treasury_mapping_postgres, upsert_classification_rows_postgres
from services.ledger_report_source import invalidate_cashflow_summary_read_models, prepare_cashflow_summary_snapshot
from services.cashflow_preparation import (
    enqueue_cashflow_preparation_job,
    run_cashflow_preparation_job_postgres,
)
from services.cashflow_voucher_occurrence_store import resolve_upload_occurrences_postgres
from typing import List, Optional
from pydantic import BaseModel, StrictInt, StrictBool
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo
from routers.auth import get_current_user
from services.storage_cleanup import purge_replaced_transaction_rows, DataPurgeProtectedError, _remove_staged_upload_files
from services.upload_lifecycle import (
    declared_coverage, scope_sql, source_fingerprint, match_unchanged, digest,
    get_receipt, prepare_lifecycle, guard_lifecycle, finish_lifecycle,
    SOURCE_ROW_HASH_SQL,
    unbacked_uploads_allowed, apply_upload_backup_policy,
)
from services.upload_reconciliation import (
    footing_columns, selected_control_row, reconcile_source, summarize_rows,
)

# ⭐ MUST HAVE THIS LINE
router = APIRouter()
logger = logging.getLogger(__name__)
UPLOAD_COMPARISON_PAGE_SIZE = 2000
# Opt-in, request-local context; normal uploads never inherit ledger behaviour.
_ledger_context = ContextVar("account_ledger_upload_context", default=None)
_source_row_numbers = ContextVar("upload_source_row_numbers", default=None)
LEDGER_SOURCE_NOTES = 'Ledger Source Notes'


def _ledger_decimal(value, *, balance=False):
    """Parse financial controls exactly, preserving zero and debit/credit signs."""
    if value is None or (not isinstance(value, str) and pd.isna(value)) or str(value).strip() == "":
        return None
    # Native Excel numbers may stringify in scientific notation; text exports
    # still require an unambiguous decimal/thousands layout.
    text = (format(Decimal(str(value)), "f") if isinstance(value, (int, float, Decimal)) and not isinstance(value, bool)
            else str(value).strip().upper())
    if balance:
        # ERP presentation varies; preserve raw cells and normalize only for arithmetic.
        prefix = re.fullmatch(r'(DR|CR)\s*(.+)', text)
        if prefix:
            text = prefix[2].strip() + ' ' + prefix[1]
        parentheses = re.fullmatch(r'\(([^()]+)\)(\s*(?:DR|CR))?', text)
        if parentheses:
            text = '-' + parentheses[1].strip() + (parentheses[2] or '')
    match = re.fullmatch(r"([+-]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?)(?:\s*(DR|CR))?", text)
    if not match or (match[2] and not balance):
        raise HTTPException(400, "Enter a valid number. Only balances may include DR or CR.")
    try:
        number = Decimal(match[1].replace(",", ""))
    except InvalidOperation as error:
        raise HTTPException(400, "Invalid ledger amount") from error
    if not number.is_finite() or (not balance and number < 0):
        raise HTTPException(400, "Debit and credit must be finite, non-negative amounts.")
    if match[2]:
        if match[2] == "DR" and number < 0:
            raise HTTPException(400, "Balance sign conflicts with the DR label.")
        number = abs(number) * (-1 if match[2] == "CR" else 1)
    return number


def _ledger_account_choices(client_id, account_code=None):
    """Use every posting ledger in this client's latest parsed Trial Balance."""
    account_filter = 'AND a.account_code = %s' if account_code is not None else ''
    params = (account_code, client_id) if account_code is not None else (client_id,)
    rows = execute_query_pg(
        f"""SELECT a.account_code, a.account_name, a.classification
           FROM trial_balance_accounts a
           WHERE a.classification = 'leaf'
             {account_filter}
             AND a.upload_id = (
                 SELECT id FROM trial_balance_uploads WHERE client_id = %s AND status = 'parsed'
                 ORDER BY (period_end IS NULL), period_end DESC, uploaded_at DESC LIMIT 1)
           ORDER BY a.account_name, a.account_code""", params)
    return rows


@router.get('/api/upload/ledger-accounts')
def get_ledger_upload_accounts(client_id: int):
    if not execute_query_pg('SELECT id FROM clients WHERE id = %s AND is_active = TRUE', (client_id,)):
        raise HTTPException(404, 'Client not found or inactive')
    accounts = _ledger_account_choices(client_id)
    if len({r['account_code'] for r in accounts}) != len(accounts):
        raise HTTPException(409, 'The latest Trial Balance has duplicate posting account codes. Resolve them before upload.')
    return {'accounts': [{'account_code': r['account_code'], 'account_name': r['account_name']} for r in accounts]}


def _resolve_ledger_context(request, template, *, lock=False):
    """Validate upload-level identity against the same central account sources."""
    if template.get("data_purpose", "standard") != "account_ledger":
        if request.ledger_account_code or request.ledger_period_start or request.ledger_period_end:
            raise HTTPException(400, "Account context requires an Account Ledger template")
        return None
    code = (request.ledger_account_code or "").strip()
    if not code or len(code) > 120:
        raise HTTPException(400, "Choose a Trial Balance posting account.")
    try:
        start = datetime.strptime(request.ledger_period_start or "", "%Y-%m-%d").date().isoformat()
        end = datetime.strptime(request.ledger_period_end or "", "%Y-%m-%d").date().isoformat()
    except ValueError as error:
        raise HTTPException(400, "Enter valid ledger coverage dates.") from error
    if start > end:
        raise HTTPException(400, "Coverage start must be on or before the end date.")
    if request.edited_cells or request.force_include_rows:
        raise HTTPException(400, "Fix rejected or blank ledger rows in Excel before upload.")
    accounts = _ledger_account_choices(request.client_id, code)
    if len(accounts) != 1:
        raise HTTPException(400, "Choose an account from the latest Trial Balance.")
    roles = template.get("ledger_columns") or {}
    if isinstance(roles, str):
        roles = json.loads(roles)
    required = {"voucher", "date", "description", "debit", "credit", "balance"}
    if set(roles) != required or len(set(roles.values())) != len(required):
        raise HTTPException(400, "Set all six ledger column roles in the template.")
    column_lock = " FOR SHARE OF m, p" if lock else ""
    columns = execute_query_pg(
        """SELECT m.id, m.column_name, m.data_type, p.reconciliation_role, p.compare_for_changes, p.comparison_review_required FROM template_column_mappings p
           JOIN master_columns m ON m.id = p.column_id AND m.is_active = TRUE
           WHERE p.template_id = %s ORDER BY p.sort_order""" + column_lock, (request.template_id,),
    )
    by_id = {row["id"]: row for row in columns}
    mapped = {item.template_column_id: item.excel_column_name for item in request.mappings}
    if not columns or columns[0]["id"] != roles["voucher"] or any(
        column not in by_id or not mapped.get(column) for column in roles.values()
    ):
        raise HTTPException(400, "Map all six ledger columns. Put Voucher first.")
    if any(column['column_name'].casefold() == LEDGER_SOURCE_NOTES.casefold() for column in columns):
        raise HTTPException(400, f"Rename '{LEDGER_SOURCE_NOTES}'; that name is reserved for attached ledger notes.")
    allowed = {"voucher": {"Text"}, "date": {"Date"}, "description": {"Text"},
               "debit": {"Numeric"}, "credit": {"Numeric"}, "balance": {"Text", "Numeric"}}
    if any(by_id[column]["data_type"] not in allowed[role] for role, column in roles.items()):
        raise HTTPException(400, "Ledger template column types do not match their roles.")
    # Configuration participates in the plan guard, including shared column types.
    configuration = hashlib.sha256(json.dumps([roles, columns], sort_keys=True).encode()).hexdigest()
    return {"account_code": code, "account_name": accounts[0]["account_name"],
            "start": start, "end": end, "configuration": configuration,
            "roles": {role: by_id[column]["column_name"] for role, column in roles.items()},
            "excel_roles": {role: mapped[column] for role, column in roles.items()},
            "row_numbers": {}, "evidence": {"opening": None, "closing": None, "control_rows": []}}


def _with_ledger_context(function):
    """Reset context even on failed validation/import or a concurrent request."""
    @wraps(function)
    def wrapped(request, *args, **kwargs):
        apply_upload_backup_policy(request, execute_query_pg)
        templates = execute_query_pg(
            "SELECT id, data_purpose, ledger_columns FROM templates WHERE id = %s AND is_active = TRUE",
            (request.template_id,),
        )
        context = _resolve_ledger_context(request, templates[0]) if templates else None
        token = _ledger_context.set(context)
        source_token = _source_row_numbers.set({})
        try:
            if context:
                files = execute_query_pg("SELECT status, client_id FROM uploaded_files WHERE id = %s", (request.file_id,))
                if files and files[0].get('status') == 'Imported' and request.validation_plan_id and function.__name__ == 'validate_and_import':
                    completed = get_receipt(request, execute_query_pg, _validation_request_signature(request))
                    if completed:
                        return completed
                if not files or files[0].get("status") == "Imported":
                    raise HTTPException(409, "Upload a fresh ledger file; an imported source cannot be reassigned")
                if files[0].get("client_id") not in (None, request.client_id):
                    raise HTTPException(409, "The staged ledger belongs to a different client")
            if context and function.__name__ == "validate_and_import" and not request.validation_plan_id:
                raise HTTPException(400, "Validate the account ledger first and import using its validation plan")
            if context and function.__name__ == "check_duplicates":
                raise HTTPException(400, "Use Validate for the account-specific ledger duplicate check")
            if request.comparison_mode == 'replace_range':
                selected = declared_coverage(request.coverage_start or (context or {}).get('start'),
                                             request.coverage_end or (context or {}).get('end'))
                if context and selected != {'start': context['start'], 'end': context['end']}:
                    raise HTTPException(400, 'Complete ledger coverage must match the declared ledger source period.')
                if function.__name__ == 'validate_and_import' and not request.validation_plan_id:
                    raise HTTPException(400, 'Validate the complete report before importing its replacement plan.')
            return function(request, *args, **kwargs)
        finally:
            _source_row_numbers.reset(source_token)
            _ledger_context.reset(token)
    return wrapped


def _ledger_row_number(row, fallback):
    context = _ledger_context.get()
    source_number = (_source_row_numbers.get() or {}).get(id(row), fallback)
    return context["row_numbers"].get(id(row), source_number) if context else source_number


def _source_order_pairs(incoming, matched, exact, equivalent):
    """Attach positions to already-matched occurrences, never infer corrections.

    Reserve exact matches first, as the shared matcher does. Identical repeated
    lines consume distinct existing IDs in order; raw values/identity stay intact.
    """
    from collections import defaultdict, deque
    remaining = list(incoming)
    available = {old['client_data_id']: old for old in matched}
    pairs = []
    for key in (exact, equivalent):
        buckets = defaultdict(deque)
        for old in available.values():
            buckets[key(old, True)].append(old)
        next_remaining = []
        for new in remaining:
            bucket = buckets[key(new, False)]
            if not bucket:
                next_remaining.append(new)
                continue
            old = bucket.popleft()
            available.pop(old['client_data_id'])
            pairs.append({'client_data_id': old['client_data_id'],
                'source_row_number': new['source_row_number'],
                'expected_old_version': old['old_version'],
                'expected_old_fingerprint': _row_fingerprint_token(old['old_row_data']),
                'expected_source_row_number': old.get('source_row_number'),
                'expected_source_order_upload_id': old.get('source_order_upload_id')})
        remaining = next_remaining
    if available:
        raise HTTPException(409, 'Source row order could not be paired safely. Validate the file again.')
    return pairs


def _apply_source_order_updates(conn, request, actions):
    """Guard and refresh compact positions without touching transaction values."""
    if not actions:
        return
    ids = [item['client_data_id'] for item in actions]
    if len(ids) != len(set(ids)):
        raise HTTPException(409, 'Source order plan contains repeated row identities. Validate again.')
    current = execute_query_pg('''SELECT id, row_data, version, source_row_number, source_order_upload_id
        FROM client_data WHERE id = ANY(%s) AND client_id = %s AND template_id = %s
          AND is_deleted = FALSE AND COALESCE(change_detection_status, 'active') != 'deleted' FOR UPDATE''',
        (ids, request.client_id, request.template_id))
    by_id = {r['id']: r for r in current}
    for item in actions:
        row = by_id.get(item['client_data_id'])
        raw = row.get('row_data') if row else None
        if isinstance(raw, str):
            raw = json.loads(raw)
        if (not row or row.get('version', 1) != item['expected_old_version']
                or _row_fingerprint_token(raw) != item['expected_old_fingerprint']
                or row.get('source_row_number') != item.get('expected_source_row_number')
                or row.get('source_order_upload_id') != item.get('expected_source_order_upload_id')
                or not isinstance(item.get('source_row_number'), int) or item['source_row_number'] < 2):
            raise HTTPException(409, 'Data or source row order changed after validation. Validate again; no data was changed.')
    execute_batch_pg(conn, '''UPDATE client_data SET source_row_number = %s, source_order_upload_id = %s
        WHERE id = %s AND client_id = %s AND template_id = %s AND is_deleted = FALSE''',
        [(a['source_row_number'], request.file_id, a['client_data_id'], request.client_id, request.template_id) for a in actions])


def _prepare_source_rows(frame, request, template_cols, col_mapping):
    """Recognise controls from every source cell before projecting mapped columns.

    A total word alone is insufficient: document/date/account evidence always
    wins. Source positions remain separate from stored transaction values.
    """
    records = frame.to_dict('records')
    try:
        control_row = selected_control_row(records, template_cols, col_mapping,
                                          request.control_total_row, request.control_total_confirmed)
    except ValueError as error:
        raise HTTPException(400, str(error)) from error
    pattern = re.compile(r'(?:grand\s*)?(?:sub\s*)?total\s*:?$', re.I)
    def blank(value):
        return value is None or pd.isna(value) or str(value).strip() == ''

    def marker(value):
        return isinstance(value, str) and bool(pattern.fullmatch(value.strip()))

    context = _ledger_context.get()
    roles = (context or {}).get('excel_roles', {})
    if context:
        context['note_candidates'] = {}
    key = roles.get('voucher') or col_mapping.get(resolve_document_column(template_cols)['column_name'])
    protected = {col_mapping.get(c['column_name']) for c in template_cols
                 if c['data_type'] == 'Date' or 'account' in c['column_name'].casefold()}
    protected.discard(None)
    rows, controls = [], []
    positions = _source_row_numbers.get()
    for number, raw in enumerate(records, 2):
        if number == control_row:
            controls.append({'row_number': number, 'kind': 'confirmed_total', 'label': 'Excel control total',
                             'amounts': {c['column_name']: str(raw[col_mapping[c['column_name']]])
                                         for c in footing_columns(template_cols)}})
            continue
        nonblank = [value for value in raw.values() if not blank(value)]
        summary = (any(marker(value) for value in nonblank)
                   and (blank(raw.get(key)) or marker(raw.get(key)))
                   and all(blank(raw.get(column)) or marker(raw.get(column)) for column in protected))
        # Extra source text/dates are evidence of an ambiguous or genuine row.
        if summary:
            for value in nonblank:
                if marker(value):
                    continue
                try:
                    if isinstance(value, bool) or not Decimal(str(value).replace(',', '')).is_finite():
                        summary = False
                except (InvalidOperation, ValueError, TypeError):
                    summary = False
        if not nonblank or summary:
            controls.append({'row_number': number, 'kind': 'total' if summary else 'blank',
                             'label': next((v.strip() for v in nonblank if marker(v)), ''),
                             'amounts': {name: str(raw.get(col_mapping.get(name)))
                                         for name in ('Debit Amount', 'Credit Amount')
                                         if col_mapping.get(name) and not blank(raw.get(col_mapping[name]))}})
            continue
        row = {header: raw[header] for header in dict.fromkeys(col_mapping.values()) if header in raw}
        if context:
            # Inspect unmapped cells too: projecting away an amount/reference
            # must never turn an ambiguous source row into a harmless note.
            context['note_candidates'][id(row)] = _ledger_description_only(raw, roles)
        rows.append(row)
        if positions is not None:
            positions[id(row)] = number

    diagnostics = {'excluded_count': len(controls), 'excluded_rows': controls, 'warnings': []}
    # Compare only a single trailing report total, never subtotals or rates.
    totals = [c for c in controls if c['kind'] == 'total']
    if len(totals) == 1 and not _ledger_context.get() and not footing_columns(template_cols):
        total = totals[0]
        last_transaction = max((positions or {}).values(), default=1)
        if total['row_number'] > last_transaction and 'sub' not in total['label'].casefold():
            for name, reported in total['amounts'].items():
                try:
                    actual = sum((Decimal(str(r[col_mapping[name]]).replace(',', ''))
                                  for r in rows if not blank(r.get(col_mapping[name]))), Decimal(0))
                    expected = Decimal(reported.replace(',', ''))
                    if actual.is_finite() and expected.is_finite() and actual != expected:
                        diagnostics['warnings'].append({'row_number': total['row_number'], 'column': name,
                            'message': f'Source footer {expected} differs from transaction total {actual} by {actual - expected}. Amounts were not altered.'})
                except (InvalidOperation, ValueError, TypeError):
                    pass  # Invalid transaction amounts are reported by normal validation.
    return rows, diagnostics


def _source_reconciliation(frame, rows, request, columns, mapping):
    """Run once before user exclusions/edits; retain only compact controls."""
    if footing_columns(columns) and request.edited_cells:
        raise HTTPException(400, 'Correct Footing-controlled uploads in Excel, then select the revised file.')
    try:
        footer_values = frame.iloc[request.control_total_row - 2].to_dict() if footing_columns(columns) else {}
        return reconcile_source(footer_values, rows, columns, mapping,
                                request.control_total_row, _ledger_row_number)
    except ValueError as error:
        raise HTTPException(400, str(error)) from error


def _reconciliation_failure(controls, total_rows):
    errors = [{'row_number': controls['control_total_row'], 'row_data': {}, 'errors': [
        {'column': check['column'], 'error_type': 'footing_mismatch',
         'message': f"Excel total {check['excel_total']} differs from detail total {check['detail_total']} by {check['difference']}. Correct the source workbook."}
        for check in controls['checks'] if Decimal(check['difference']) != 0]}]
    result = _source_input_failure(errors, total_rows)
    result['source_reconciliation'] = controls
    return result


def _approved_reconciliation(controls, rows, columns, mapping, request, date_range):
    """Approved detail can be a subset; its totals never replace the source footer."""
    try:
        controls['approved_detail'] = summarize_rows(rows, columns, mapping, _ledger_row_number)
    except ValueError as error:
        raise HTTPException(400, str(error)) from error
    controls['scope'] = {'client_id': request.client_id, 'template_id': request.template_id,
                         'comparison_mode': request.comparison_mode, 'date_range': date_range,
                         'ledger_account_code': request.ledger_account_code,
                         'complete_period': request.comparison_mode == 'replace_range'}
    controls['omitted_detail_rows'] = controls['full_sheet']['row_count'] - len(rows)
    return controls


def _workbook_input_errors(file_path, request, template_cols, col_mapping, frame):
    """Inspect cell types before pandas can turn Excel errors/NaN text into blanks.

    Only mapped, included cells participate. Valid values and all identity and
    comparison rules remain untouched; this is a source-validation boundary.
    """
    headers = list(frame.columns)
    mapped = {}
    for column in template_cols:
        source = col_mapping.get(column['column_name'])
        if source in headers:
            mapped.setdefault(headers.index(source), []).append(column)
    if not mapped:
        return []
    controlled = bool(footing_columns(template_cols))
    excluded = set(request.excluded_rows or []) if request.comparison_mode != 'replace_range' and not controlled else set()

    def inspect(number, cells):
        if number in excluded:
            return None
        errors, values = [], {}
        for position, columns in mapped.items():
            value, excel_error = cells.get(position, (None, False))
            for column in columns:
                safe = json_safe_cell_value(value)
                if isinstance(safe, (float, Decimal)) and not math.isfinite(float(safe)):
                    safe = str(safe)
                values[column['column_name']] = safe
                message, kind = None, 'data_type'
                if controlled and isinstance(value, str) and value.startswith('='):
                    # pandas already supplies cached results. Inspect formula text
                    # in this same metadata pass so a missing cache is not a zero.
                    cached = frame.iloc[number - 2, position] if number - 2 < len(frame) else None
                    if cached is None or pd.isna(cached) or str(cached).strip() == '':
                        message, kind = 'Recalculate and save this formula in Excel, then upload again.', 'formula_result_missing'
                    value = cached
                if excel_error:
                    message, kind = 'Fix the Excel formula error.', 'excel_error'
                elif not message and column['data_type'] == 'Numeric' and value is not None and str(value).strip():
                    try:
                        if not math.isfinite(float(value)):
                            message = 'Enter a valid number.'
                    except (TypeError, ValueError, OverflowError):
                        pass  # Ordinary numeric-format errors use the existing validator.
                if message:
                    errors.append({'column': column['column_name'],
                                   'excel_column': col_mapping[column['column_name']],
                                   'error_type': kind, 'message': message})
        return {'row_number': number, 'row_data': values, 'errors': errors} if errors else None

    errors = []
    if str(file_path).casefold().endswith('.xls'):
        import xlrd
        book = xlrd.open_workbook(file_path, on_demand=True)
        try:
            sheet = book.sheet_by_name(request.sheet_name)
            for index in range(1, sheet.nrows):
                cells = {pos: (sheet.cell_value(index, pos), sheet.cell_type(index, pos) == xlrd.XL_CELL_ERROR)
                         for pos in mapped if pos < sheet.ncols}
                error = inspect(index + 1, cells)
                if error:
                    errors.append(error)
        finally:
            book.release_resources()
    else:
        book = openpyxl.load_workbook(file_path, read_only=True, data_only=not controlled)
        try:
            sheet = book[request.sheet_name]
            for number, row in enumerate(sheet.iter_rows(min_row=2, max_col=max(mapped) + 1), 2):
                error = inspect(number, {pos: (row[pos].value, row[pos].data_type == 'e') for pos in mapped})
                if error:
                    errors.append(error)
        finally:
            book.close()
    return errors


def _source_input_failure(errors, total_rows):
    """Use the existing row-error response so the UI can link to each source cell."""
    return {'status': 'validation_failed', 'has_errors': True, 'can_import': False,
            'error_count': len(errors), 'errors': errors, 'total_rows': total_rows,
            'has_rejections': False, 'rejected_count': 0, 'rejected_rows': [],
            'message': 'Fix the listed rows and validate again. No data was changed.'}


def _ledger_description_only(row, roles):
    description = roles['description']
    text = row.get(description)
    return (isinstance(text, str) and bool(text.strip())
            and all(classify_date_cell(value) == 'empty'
                    for key, value in row.items() if key != description))


def _prepare_ledger_rows(rows, request):
    """Extract controls before user exclusions; retain original transaction values."""
    context = _ledger_context.get()
    if not context:
        return rows
    roles = context["excel_roles"]
    transactions = []
    balance_rows = []
    incomplete = False
    excluded = set(request.excluded_rows or [])
    context['row_notes'] = {}
    context['evidence']['continuation_rows'] = []
    parent, previous_number = None, None
    for number, row in enumerate(rows, 2):
        number = _ledger_row_number(row, number)
        voucher = row.get(roles["voucher"])
        blank_voucher = voucher is None or pd.isna(voucher) or not str(voucher).strip()
        label = re.sub(r"[^a-z]", "", str(row.get(roles["description"]) or "").casefold())
        kind = ("opening" if label in {"openingbalance", "balanceforward", "balancebroughtforward"}
                else "closing" if label == "closingbalance" else None)
        if blank_voucher and kind:
            value = _ledger_decimal(row.get(roles["balance"]), balance=True)
            if context["evidence"][kind] is not None or any(
                item["kind"] == kind for item in context["evidence"]["control_rows"]
            ):
                raise HTTPException(400, f"Only one {kind} balance row is allowed.")
            context["evidence"][kind] = str(value) if value is not None else None
            context["evidence"]["control_rows"].append({"row_number": number, "kind": kind})
            parent, previous_number = None, number
            continue
        note = context.get('note_candidates', {}).get(id(row), _ledger_description_only(row, roles))
        if note:
            if parent is None or previous_number != number - 1 or number in excluded:
                raise HTTPException(400, f"Ledger note at row {number} must follow an included transaction. Keep notes with their voucher.")
            text = row[roles['description']]
            notes = context['row_notes'].setdefault(id(parent), [])
            notes.append(text)
            context['evidence']['continuation_rows'].append({
                'row_number': number, 'parent_row_number': _ledger_row_number(parent, number - 1),
                'voucher': str(parent[roles['voucher']])})
            previous_number = number
            continue
        # A voucher-bearing description saying 'Closing balance' is a real line.
        context["row_numbers"][id(row)] = number
        parent, previous_number = None, number
        if number not in excluded:
            amounts = {}
            for role in ("debit", "credit", "balance"):
                amounts[role] = _ledger_decimal(row.get(roles[role]), balance=role == "balance")
            value = row.get(roles["date"])
            if classify_date_cell(value) != "empty":
                if classify_date_cell(value) == "native":
                    day = value.date().isoformat()
                elif classify_date_cell(value) == "excel_serial":
                    day = excel_serial_to_datetime(value).date().isoformat()
                else:
                    try:
                        day = parse_date_flexible(value, request.date_delimiter if request.date_delimiter is not None else DEFAULT_DATE_DELIMITER,
                                                  request.date_order or DEFAULT_DATE_ORDER).date().isoformat()
                    except (ValueError, TypeError) as error:
                        raise HTTPException(400, f"Enter a valid date on Excel row {number}.") from error
                if not context["start"] <= day <= context["end"]:
                    raise HTTPException(400, f"Row {number}: {voucher} dated {day} is outside {context['start']}–{context['end']}. Widen coverage or correct the date.")
                if blank_voucher or (amounts['debit'] is None and amounts['credit'] is None):
                    incomplete = True
                else:
                    parent = row
                balance_rows.append({'row_number': number, 'day': day, **amounts})
            else:
                incomplete = True
        transactions.append(row)
    context['balance_rows'] = balance_rows  # Request-only; never persist transaction copies here.
    context['balance_incomplete'] = incomplete
    context['balance_mode'] = request.comparison_mode
    _calculate_ledger_balances(context)
    return transactions


def _calculate_ledger_balances(context, previous=None):
    """Keep ERP controls intact and retain only compact derived reconciliation evidence."""
    evidence = context['evidence']
    rows = context.get('balance_rows', [])
    check = {'opening': None, 'closing': None, 'opening_source': 'unavailable',
             'checked_balances': 0, 'difference_count': 0, 'differences': [],
             'continuity': previous or {'status': 'not_available'}}
    evidence['balance_check'] = check
    if context.get('balance_mode') != 'replace_range' or context.get('balance_incomplete'):
        check['status'] = 'partial_or_incomplete'
        return
    # Dates establish direction; evaluate a reverse export back to front.
    days = [r['day'] for r in rows]
    if days != sorted(days):
        if days == sorted(days, reverse=True):
            rows = list(reversed(rows))
            check['order'] = 'reverse_source'
        else:
            check['status'] = 'unordered'
            return
    else:
        check['order'] = 'source'
    opening = _ledger_decimal(evidence['opening'], balance=True)
    if opening is not None:
        check['opening_source'] = 'erp_opening'
    else:
        movement = Decimal(0)
        for row in rows:
            movement += (row['debit'] or Decimal(0)) - (row['credit'] or Decimal(0))
            if row['balance'] is not None:
                opening = row['balance'] - movement
                check['opening_source'] = 'inferred_from_transaction'
                check['anchor_row'] = row['row_number']
                break
        if opening is None and not rows and evidence['closing'] is not None:
            opening = _ledger_decimal(evidence['closing'], balance=True)
            check['opening_source'] = 'inferred_from_closing'
        if opening is None and previous and previous.get('status') == 'available':
            opening = Decimal(previous['expected_opening'])
            check['opening_source'] = 'previous_coverage'
    if opening is None:
        check['status'] = 'opening_unavailable'
        return
    check['opening'] = str(opening)
    if previous and previous.get('status') == 'available':
        delta = opening - Decimal(previous['expected_opening'])
        check['continuity'] = {**previous, 'status': 'matched' if delta == 0 else 'difference', 'difference': str(delta)}
    running = opening
    for row in rows:
        running += (row['debit'] or Decimal(0)) - (row['credit'] or Decimal(0))
        if row['balance'] is not None:
            check['checked_balances'] += 1
            delta = running - row['balance']
            if delta:
                check['difference_count'] += 1
                if len(check['differences']) < 20:
                    check['differences'].append({'row_number': row['row_number'], 'expected': str(running),
                                                'source': str(row['balance']), 'difference': str(delta)})
    check['closing'] = str(running)
    closing = _ledger_decimal(evidence['closing'], balance=True)
    if closing is not None:
        check['closing_difference'] = str(running - closing)
        if running != closing:
            check['difference_count'] += 1
    check['status'] = 'differences' if check['difference_count'] or check['continuity']['status'] == 'difference' else 'consistent'


def _ledger_period_fingerprint(request, start, end):
    """One compact proof of current canonical rows, without a second row dataset."""
    result = execute_query_pg(
        """SELECT md5(COALESCE(string_agg(cd.id::text || ':' || COALESCE(cd.version, 1)::text || ':' ||
                      COALESCE(cd.source_fingerprint, ''), ',' ORDER BY cd.id), '')) AS fingerprint,
                  COUNT(*) FILTER (WHERE cd.source_fingerprint IS NULL) AS incomplete
           FROM client_data cd JOIN uploaded_files u ON u.id = cd.upload_id
           WHERE cd.client_id = %s AND cd.template_id = %s AND cd.is_deleted = FALSE
             AND u.ledger_account_code = %s AND cd.extract_date BETWEEN %s AND %s""",
        (request.client_id, request.template_id, _ledger_context.get()['account_code'], start, end))
    return result[0]['fingerprint'] if result and not result[0].get('incomplete') else None


def _connect_ledger_balances(request):
    """Only continuous, unchanged complete coverage can supply a previous closing."""
    context = _ledger_context.get()
    if request.comparison_mode != 'replace_range':
        return
    prior = execute_query_pg(
        """SELECT id, ledger_period_start, ledger_period_end, ledger_evidence FROM uploaded_files
           WHERE client_id = %s AND template_id = %s AND ledger_account_code = %s
             AND status = 'Imported' AND ledger_period_end < %s AND id <> %s
           ORDER BY ledger_period_end DESC, id DESC LIMIT 1""",
        (request.client_id, request.template_id, context['account_code'], context['start'], request.file_id))
    previous = {'status': 'not_available'}
    if prior:
        item = prior[0]
        end = str(item['ledger_period_end'])[:10]
        previous = {'status': 'requires_revalidation', 'previous_upload_id': item['id'], 'previous_end': end}
        if (datetime.fromisoformat(context['start']).date() - timedelta(days=1)).isoformat() != end:
            previous['status'] = 'gap'
        else:
            stored = item.get('ledger_evidence') or {}
            if isinstance(stored, str):
                stored = json.loads(stored)
            check = stored.get('balance_check') or {}
            if (not stored.get('continuity_stale') and check.get('source_mode') == 'replace_range'
                    and check.get('status') == 'consistent' and check.get('closing') is not None
                    and check.get('configuration') == context['configuration']
                    and check.get('opening_source') in {'erp_opening', 'inferred_from_transaction', 'inferred_from_closing', 'previous_coverage'}
                    and check.get('canonical_fingerprint')
                    and check['canonical_fingerprint'] == _ledger_period_fingerprint(request, str(item['ledger_period_start'])[:10], end)
                    and _ledger_ancestry_is_current(request, item, check)):
                previous.update(status='available', expected_opening=check['closing'],
                                proof_start=check.get('proof_start', str(item['ledger_period_start'])[:10]),
                                root_upload_id=check.get('root_upload_id', item['id']),
                                root_opening=check.get('root_opening', check.get('opening')))
    _calculate_ledger_balances(context, previous)


def _ledger_ancestry_is_current(request, item, check):
    """A bounded history proof also catches ancestor edits/deletions outside Upload."""
    if check['opening_source'] != 'previous_coverage':
        return True  # An independently supplied/inferred ERP anchor starts a new chain.
    start, root_id = check.get('proof_start'), check.get('root_upload_id')
    if (not start or not root_id or not check.get('history_fingerprint')
            or start > str(item['ledger_period_start'])[:10]
            or check['history_fingerprint'] != _ledger_period_fingerprint(request, start, str(item['ledger_period_end'])[:10])):
        return False
    roots = execute_query_pg(
        """SELECT id, ledger_period_start, ledger_evidence FROM uploaded_files
           WHERE id = %s AND client_id = %s AND template_id = %s AND ledger_account_code = %s AND status = 'Imported'""",
        (root_id, request.client_id, request.template_id, _ledger_context.get()['account_code']))
    if not roots or str(roots[0]['ledger_period_start'])[:10] != start:
        return False
    evidence = roots[0].get('ledger_evidence') or {}
    if isinstance(evidence, str):
        evidence = json.loads(evidence)
    root = evidence.get('balance_check') or {}
    return bool(not evidence.get('continuity_stale') and root.get('status') == 'consistent'
                and root.get('source_mode') == 'replace_range'
                and root.get('opening_source') in {'erp_opening', 'inferred_from_transaction', 'inferred_from_closing'}
                and root.get('configuration') == _ledger_context.get()['configuration']
                and root.get('opening') == check.get('root_opening'))


def _ledger_balance_warnings(context):
    check = context['evidence'].get('balance_check') or {}
    warnings = [{'row_number': item['row_number'], 'column': 'Balance',
                 'message': f"ERP balance {item['source']} differs from calculated {item['expected']} by {item['difference']}. ERP value kept."}
                for item in check.get('differences', [])]
    if Decimal(check.get('closing_difference', '0')):
        warnings.append({'column': 'Balance', 'message': f"ERP closing balance differs by {check['closing_difference']}. ERP value kept."})
    continuity = check.get('continuity') or {}
    if continuity.get('status') == 'difference':
        warnings.append({'column': 'Balance', 'message': f"Opening {check['opening']} differs from prior closing {continuity['expected_opening']} by {continuity['difference']}. ERP value kept."})
    elif continuity.get('status') == 'gap':
        warnings.append({'column': 'Balance', 'message': f"Previous period ends {continuity['previous_end']}; a period is missing. No opening balance was carried forward."})
    elif continuity.get('status') == 'requires_revalidation':
        warnings.append({'column': 'Balance', 'message': 'Previous balance is missing or outdated. Validate the prior period again.'})
    reason = {'opening_unavailable': 'No opening balance found. Add the ERP opening balance or upload continuous prior coverage.',
              'unordered': 'Dates are out of order. Use a chronological report to check balances.',
              'partial_or_incomplete': 'Balance checks need a complete-period report.'}.get(check.get('status'))
    if reason:
        warnings.append({'column': 'Balance', 'message': reason})
    return warnings


def _prepare_ledger_balance_proof(request):
    # A concurrent import between reading a prior anchor and sealing the plan
    # must not make an obsolete opening appear to belong to the newer dataset.
    before = _ledger_scope_fingerprint(request.client_id, request.template_id)
    _connect_ledger_balances(request)
    if before != _ledger_scope_fingerprint(request.client_id, request.template_id):
        raise HTTPException(409, 'Ledger data changed while checking balances. Validate again; no import was applied.')
    _ledger_context.get()['scope_fingerprint'] = before


def _ledger_evidence_refresh_required(request, prior):
    """Refresh a stale compact proof without re-inserting unchanged transactions."""
    context = _ledger_context.get()
    if (not context or not prior or request.comparison_mode != 'replace_range'
            or context['evidence'].get('balance_check', {}).get('status') != 'consistent'):
        return False
    stored = prior.get('ledger_evidence') or {}
    if isinstance(stored, str):
        stored = json.loads(stored)
    check = stored.get('balance_check') or {}
    proof = check.get('canonical_fingerprint')
    return bool(stored.get('continuity_stale') or not proof
                or check.get('configuration') != context['configuration']
                or not check.get('opening_source')
                or proof != _ledger_period_fingerprint(request, context['start'], context['end']))


def _finalize_ledger_balances(request, evidence):
    """Invalidate later proofs after any earlier import; preserve all ERP evidence."""
    context = _ledger_context.get()
    evidence = json.loads(json.dumps(evidence))  # Keep the validated/archive plan immutable.
    execute_write_pg(
        """UPDATE uploaded_files SET ledger_evidence = jsonb_set(COALESCE(NULLIF(ledger_evidence::text, '')::jsonb, '{}'::jsonb),
                  '{continuity_stale}', 'true'::jsonb, true)
           WHERE client_id = %s AND template_id = %s AND ledger_account_code = %s AND status = 'Imported'
             AND id <> %s AND ledger_period_end >= %s""",
        (request.client_id, request.template_id, context['account_code'], request.file_id, context['start']))
    evidence['balance_check']['canonical_fingerprint'] = _ledger_period_fingerprint(request, context['start'], context['end'])
    evidence['balance_check']['source_mode'] = request.comparison_mode
    evidence['balance_check']['configuration'] = context['configuration']
    check = evidence['balance_check']
    previous = check.get('continuity') or {}
    carried = check.get('opening_source') == 'previous_coverage'
    check['proof_start'] = previous['proof_start'] if carried else context['start']
    check['root_upload_id'] = previous['root_upload_id'] if carried else request.file_id
    check['root_opening'] = previous['root_opening'] if carried else check.get('opening')
    check['history_fingerprint'] = (_ledger_period_fingerprint(request, check['proof_start'], context['end'])
                                    if carried else check['canonical_fingerprint'])
    execute_write_pg('UPDATE uploaded_files SET ledger_evidence = %s WHERE id = %s',
                     (json.dumps(evidence), request.file_id))


def _ledger_scope_fingerprint(client_id, template_id, locked=False):
    """Guard the entire account population, including concurrent balance-only imports."""
    context = _ledger_context.get()
    records = execute_query_pg(
        """SELECT cd.id, cd.version, cd.row_data FROM client_data cd
           JOIN uploaded_files u ON u.id = cd.upload_id
           WHERE cd.client_id = %s AND cd.template_id = %s AND cd.is_deleted = FALSE
             AND u.ledger_account_code = %s ORDER BY cd.id""",
        (client_id, template_id, context["account_code"]), **({} if locked else {'page_size': UPLOAD_COMPARISON_PAGE_SIZE}),
    )
    uploads = execute_query_pg(
        """SELECT id, ledger_period_start, ledger_period_end, ledger_evidence FROM uploaded_files
           WHERE client_id = %s AND template_id = %s AND ledger_account_code = %s
             AND status = 'Imported' ORDER BY id""", (client_id, template_id, context["account_code"]),
    )
    return hashlib.sha256(json.dumps([records, uploads], sort_keys=True, default=str).encode()).hexdigest()


def _persist_ledger_evidence(request, evidence):
    context = _ledger_context.get()
    execute_write_pg(
        """UPDATE uploaded_files SET template_id = %s, ledger_account_code = %s, ledger_account_name = %s,
           ledger_period_start = %s, ledger_period_end = %s, ledger_opening_balance = %s,
           ledger_closing_balance = %s, ledger_evidence = %s WHERE id = %s""",
        (request.template_id, context["account_code"], context["account_name"], context["start"], context["end"],
         evidence["opening"], evidence["closing"], json.dumps(evidence), request.file_id),
    )


def _guard_ledger_plan(request, payload):
    """Check account/configuration and full-source freshness under the import lock."""
    context = _ledger_context.get()
    expected = payload.get("ledger")
    if not context:
        if expected:
            raise HTTPException(409, "The ledger template changed after validation")
        return
    if not expected or any(expected.get(key) != context[key] for key in
                           ("account_code", "start", "end", "configuration")):
        raise HTTPException(409, "Ledger account, coverage or configuration changed; validate again")
    templates = execute_query_pg(
        "SELECT data_purpose, ledger_columns FROM templates WHERE id = %s AND is_active = TRUE FOR SHARE", (request.template_id,)
    )
    fresh = _resolve_ledger_context(request, templates[0], lock=True) if templates else None
    if not fresh or fresh["configuration"] != context["configuration"]:
        raise HTTPException(409, "Ledger configuration changed after validation; validate again")
    if _ledger_scope_fingerprint(request.client_id, request.template_id, locked=True) != expected.get("scope_fingerprint"):
        raise HTTPException(409, "The account ledger changed after validation; validate again")
    files = execute_query_pg(
        "SELECT status, client_id, template_id FROM uploaded_files WHERE id = %s FOR UPDATE", (request.file_id,)
    )
    if (not files or files[0].get("status") != "Validated"
            or files[0].get("client_id") != request.client_id
            or files[0].get("template_id") != request.template_id):
        raise HTTPException(409, "The staged ledger changed after validation; validate again")


def _check_upload_storage_budget(additional_bytes=0):
    """Leave room for transaction/index growth and temporary validation payloads."""
    budget = get_database_storage_budget_pg(additional_bytes)
    if budget["blocked"]:
        raise HTTPException(507, detail={
            "message": "Upload stopped to keep the database below the 400 MB safety limit. Archive approved data before retrying.",
            **budget,
        })
    return budget


def _with_storage_status(result):
    """Expose the warning through the existing upload message/response contract."""
    if isinstance(result, dict):
        try:
            budget = get_database_storage_budget_pg()
            result["storage_budget"] = budget
            if budget["warning"]:
                result["message"] = (result.get("message", "") + " Database storage is above the 300 MB warning threshold.").strip()
        except Exception:
            # A post-commit measurement must not turn a saved import into a 500.
            logger.warning("Upload storage measurement unavailable")
    return result


def _build_upload_storage_path(original_filename: str) -> str:
    safe_name = os.path.basename(original_filename or 'upload.xlsx')
    configured_dir = os.environ.get('UPLOAD_STAGING_DIR')
    app_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    staging_dir = configured_dir or os.path.join(app_root, 'uploads')
    if not os.path.isabs(staging_dir):
        staging_dir = os.path.join(app_root, staging_dir)
    staging_dir = os.path.abspath(staging_dir)
    os.makedirs(staging_dir, exist_ok=True)
    return os.path.join(staging_dir, f'{uuid4().hex}_{safe_name}')


def _require_staged_upload(file_path: str) -> str:
    """Fail safely when a database upload reference outlives its staged workbook."""
    if not file_path or not os.path.isfile(file_path):
        raise HTTPException(410, detail={
            'code': 'staged_file_missing',
            'message': 'The staged file is missing. Select it again and validate before importing.',
        })
    return file_path


def _log_upload_timing(stage):
    """Opt-in timing for upload diagnostics; normal requests pay no timing-log cost."""
    def decorate(function):
        @wraps(function)
        def timed(*args, **kwargs):
            perf_enabled = kwargs.pop("perf", False)
            if not perf_enabled:
                return _with_storage_status(function(*args, **kwargs))
            token = start_db_metrics()
            started = perf_counter()
            try:
                return _with_storage_status(function(*args, **kwargs))
            finally:
                metrics = get_db_metrics()
                query_details = get_db_query_details()
                stop_db_metrics(token)
                logger.warning(
                    "upload_perf stage=%s total_ms=%.1f db_calls=%s db_reads=%s db_writes=%s db_ms=%.1f db_details=%s",
                    stage,
                    (perf_counter() - started) * 1000,
                    metrics["calls"],
                    metrics["reads"],
                    metrics["writes"],
                    metrics["db_time"] * 1000,
                    json.dumps(query_details, ensure_ascii=True),
                )
        return timed
    return decorate


def _cashflow_occurrence_input_rows(rows, col_mapping, template_cols, rate_source, rate_precision):
    """Build the complete incoming workbook representation for DV-3.

    This intentionally includes otherwise unchanged rows, because they are part
    of the voucher occurrence uploaded for audit and duplicate resolution.
    """
    return [
        _canonical_import_row_data(
            row, col_mapping, template_cols, rate_source, rate_precision
        )
        for row in rows
    ]


UPLOAD_PREPARATION_WARNING = (
    "Data saved. The Cashflows update could not be queued, so Cashflows reports "
    "use the direct calculation until it is rebuilt."
)


def _enqueue_upload_preparation(request, cashflow_voucher_resolution):
    """Queue the post-import Cashflows job without turning a saved import into a 500.

    The import rows are already committed when this runs, so a queueing failure must
    not be reported as a failed upload (that invites a needless retry). It is logged
    and returned as a warning; the legacy snapshot refresh still runs afterwards.
    """
    try:
        with pg_transaction() as conn:
            job = enqueue_cashflow_preparation_job(
                conn,
                client_id=request.client_id,
                reason="incremental_upload",
                affected_upload_ids=[request.file_id],
                affected_occurrence_ids=cashflow_voucher_resolution.get("affected_occurrence_ids", []),
                current_occurrence_ids=cashflow_voucher_resolution.get("current_occurrence_ids", []),
            )
        return job, None
    except Exception as error:
        logger.warning(
            "cashflows_prepared_job_enqueue_failed client_id=%s file_id=%s error=%s",
            request.client_id,
            request.file_id,
            error,
        )
        try:
            log_error(
                client_id=request.client_id,
                module="Upload",
                endpoint="/api/upload/import",
                method="POST",
                status_code=200,
                error_category="Cashflows preparation",
                error_message=f"Import saved; Cashflows job not queued: {error}",
                input_data={"file_id": request.file_id, "template_id": request.template_id},
            )
        except Exception as log_failure:
            logger.warning("cashflows_prepared_job_enqueue_log_failed error=%s", log_failure)
        return None, UPLOAD_PREPARATION_WARNING


def _prepare_cashflow_snapshot_background(
    client_id: int,
    file_id: int,
    template_id: int,
    cashflow_voucher_resolution: Optional[dict] = None,
    preparation_job_id: Optional[int] = None,
):
    """Run the durable preparation job and refresh the legacy fallback after import."""
    preparation_status = "complete"
    try:
        if preparation_job_id:
            prepared_result = run_cashflow_preparation_job_postgres(preparation_job_id)
            preparation_status = "complete" if prepared_result.get("status") == "completed" else str(prepared_result.get("status") or "failed")
            logger.info(
                "cashflows_prepared_job client_id=%s template_id=%s file_id=%s job_id=%s result=%s",
                client_id,
                template_id,
                file_id,
                preparation_job_id,
                json.dumps(prepared_result, default=str, ensure_ascii=True),
            )
    except Exception as error:
        preparation_status = "failed"
        logger.warning(
            "cashflows_prepared_job_failed client_id=%s template_id=%s file_id=%s job_id=%s error=%s",
            client_id,
            template_id,
            file_id,
            preparation_job_id,
            error,
        )
    try:
        prepare_cashflow_summary_snapshot(client_id)
    except Exception as error:
        logger.warning(
            "cashflows_snapshot_preparation_failed client_id=%s template_id=%s error=%s",
            client_id,
            template_id,
            error,
        )
    finally:
        progress = IMPORT_PROGRESS.get(file_id)
        if progress is not None:
            progress["status"] = preparation_status

# In-memory progress for an import, keyed by file_id. Row writes finish inside the
# request; Cashflows snapshot preparation continues as a bounded background task.
IMPORT_PROGRESS: dict = {}

# ---------- FLEXIBLE DATE PARSING (Phase 1 of the date-format requirement) ----------
# Default date-import format. There is no UI to choose a different format yet
# (that's a later phase), so these defaults reproduce today's DD/MM/YYYY-only
# behavior exactly. A later phase will let the caller override these per
# upload session instead of always using the default.
DEFAULT_DATE_DELIMITER = "/"
DEFAULT_DATE_ORDER = "DMY"  # one of DMY, MDY, YMD

def parse_date_flexible(value, delimiter: str = DEFAULT_DATE_DELIMITER, order: str = DEFAULT_DATE_ORDER) -> datetime:
    """
    Parse a date string using an explicit delimiter + component order
    (e.g. delimiter="/", order="DMY" -> "25/12/2025").
    Raises ValueError with a clear message on a malformed string or an
    out-of-range date (e.g. day=45) -- strptime itself enforces range checks.
    """
    order = order.upper()
    format_map = {
        "DMY": f"%d{delimiter}%m{delimiter}%Y",
        "MDY": f"%m{delimiter}%d{delimiter}%Y",
        "YMD": f"%Y{delimiter}%m{delimiter}%d",
    }
    if order not in format_map:
        raise ValueError(f"Unsupported date order '{order}'")
    try:
        return datetime.strptime(str(value).strip(), format_map[order])
    except ValueError:
        raise ValueError(f"'{value}' is not a valid date for format {order} (delimiter '{delimiter}')")


def classify_date_cell(value) -> str:
    """
    Classify a raw cell value from an uploaded Excel file for a Date-typed
    template column, so callers can decide whether to use it as-is or run
    it through parse_date_flexible().
    Returns one of:
      'native'       - already a real date (pandas/openpyxl parsed the cell
                        as a genuine Excel date, e.g. a datetime/Timestamp)
                        -- must NOT be passed to parse_date_flexible().
      'text'         - a non-empty string; needs parse_date_flexible().
      'excel_serial' - a bare number that may be an Excel date serial,
                        handled separately from the text parser.
      'empty'        - no value to classify.
    """
    # pandas NaT is also a datetime; missingness must win over the native-date test.
    if value is None or pd.isna(value):
        return 'empty'
    if isinstance(value, datetime):
        return 'native'
    if isinstance(value, str):
        return 'empty' if value.strip() == '' else 'text'
    if isinstance(value, (int, float)):
        return 'excel_serial'
    return 'text'


def excel_serial_to_datetime(value) -> datetime:
    """
    Converts a bare Excel date serial number (classify_date_cell() == 'excel_serial')
    to a datetime. Excel's epoch is 1899-12-30 (accounting for its 1900 leap-year bug).
    Single source of truth for this conversion -- reused by extract_columns_from_row()
    and the date-preview endpoint so they can never disagree.
    """
    from datetime import timedelta
    return datetime(1899, 12, 30) + timedelta(days=value)


class _NativeDateValue(datetime):
    """
    Passthrough for a genuine native Excel date cell (see json_safe_cell_value()'s
    preserve_native_dates flag). Stringifies to the same '%d/%m/%Y' text the
    non-preserving path would have produced -- via __str__, used by both
    compare_transactions()'s diff (str(new_val or '')) and json.dumps(...,
    default=str) at the storage call sites -- so change-detection and the stored
    row_data JSON stay byte-identical to before. But because it IS a real datetime
    instance, extract_columns_from_row()'s classify_date_cell() still resolves it
    via the 'native' branch instead of misparsing it against the text-column
    delimiter/order picker (which is only correct for genuinely text-based dates).
    """
    def __str__(self):
        return self.strftime('%d/%m/%Y')


# ---------- JSON-SAFE CELL COERCION (Phase 2 of the upload-reliability requirement) ----------
def json_safe_cell_value(value, date_fmt: str = '%d/%m/%Y', preserve_native_dates: bool = False):
    """
    Coerces a raw cell value (as read by pandas/openpyxl) into something safe to
    store in a row_data dict and pass straight to json.dumps(). Single source of
    truth for this coercion -- every place in this file that builds a row_data
    dict from a raw Excel row should go through this instead of repeating the
    isna/isinstance checks inline.

    This is the actual root-cause fix for a real crash (ERR-UNK-001 "Object of
    type time is not JSON serializable", seen repeatedly against /api/upload/import
    and /api/upload/validate): a Date-typed column's cell can come back from
    pandas as a bare datetime.time (e.g. a trailing totals/subtotal row's Date
    cell with odd/time-only formatting) rather than a full datetime.datetime or
    NaN/NaT. datetime.time is neither caught by pd.isna() nor by
    isinstance(value, datetime) (datetime.time does not subclass datetime.datetime),
    so it was falling through untouched into a raw row_data dict and then blowing
    up the first time json.dumps() saw it, with no error handling around it.

    preserve_native_dates=True returns a _NativeDateValue instead of stringifying
    a native datetime/Timestamp -- opt in only where the resulting row_data later
    reaches extract_columns_from_row(), so a real Excel date is extracted via its
    own value instead of being needlessly re-parsed against the text delimiter/
    order picker (see _NativeDateValue's docstring). Default False preserves
    today's behavior for every other caller.
    """
    if pd.isna(value):
        return None
    if isinstance(value, datetime):
        if preserve_native_dates:
            return _NativeDateValue(value.year, value.month, value.day,
                                     value.hour, value.minute, value.second, value.microsecond)
        return value.strftime(date_fmt)
    if isinstance(value, time):
        return value.strftime('%H:%M:%S')
    return value


# ---------- EDITED BLANK-CELL VALUES (Phase 4 of the upload-reliability requirement) ----------
def apply_edited_cell_values(rows: list, edited_cells) -> list:
    """
    Applies values the user typed into blank cells in the Row Selection grid
    (static/upload.js's editedCellValues, Phase 3) before the row is validated/imported.
    row_number matches the same scheme as filter_excluded_rows()/validation_errors[].row_number
    (header=1, first data row=2), so this MUST run before filter_excluded_rows() -- that
    request-local source positions preserve the original Excel numbers even after
    automatic control-row recognition or manual exclusion.

    Only overwrites a cell that is CURRENTLY blank -- a stray/stale edit can never clobber real
    data, even though the frontend already restricts editing to blank, non-key-column cells.
    A cell the user never edited is left exactly as read from the sheet (still blank), per the
    locked "no default value is auto-inserted" requirement.
    """
    if not edited_cells:
        return rows
    edits_by_row = {}
    for edit in edited_cells:
        edits_by_row.setdefault(edit.row_number, {})[edit.excel_column] = edit.value
    for row_idx, row in enumerate(rows, start=2):
        row_edits = edits_by_row.get(_ledger_row_number(row, row_idx))
        if not row_edits:
            continue
        for excel_column, value in row_edits.items():
            if excel_column not in row:
                continue  # not a real mapped Excel column -- ignore
            current = row[excel_column]
            is_blank = pd.isna(current) if not isinstance(current, str) else current.strip() == ''
            if is_blank:
                row[excel_column] = value
    return rows


# ---------- ROW EXCLUSION (Phase 1 of the row-exclusion requirement) ----------
def filter_excluded_rows(rows: list, excluded_rows: Optional[List[int]]) -> list:
    """
    Removes rows the user marked for exclusion before this upload session
    (row_number matches validation_errors[].row_number: header row=1, first data row=2).
    Excluded rows are session-only and never persisted -- this is the single point
    where exclusion is applied, so callers downstream (validation, import,
    document_group_id grouping) never see excluded rows at all. Returns a new
    list; None/empty excluded_rows is a no-op (identical to today's behavior).
    """
    if not excluded_rows:
        return rows
    excluded_set = set(excluded_rows)
    return [row for idx, row in enumerate(rows, start=2) if _ledger_row_number(row, idx) not in excluded_set]


def _count_new_and_existing_rows_legacy(client_id: int, template_id: int, rows: list, template_cols: list, mapping_dict: dict):
    """Legacy exact comparison used only if the set-based PostgreSQL query fails."""
    existing_rows = execute_query_pg(
        """
        SELECT row_data
        FROM client_data
        WHERE client_id = %s AND template_id = %s AND is_deleted = FALSE
        """,
        (client_id, template_id),
    )
    seen_existing = set()
    for row in existing_rows:
        raw = row.get("row_data")
        if raw is not None:
            try:
                seen_existing.add(json.dumps(raw, sort_keys=True, ensure_ascii=False))
            except Exception:
                seen_existing.add(str(raw))

    new_count = 0
    existing_count = 0
    for row in rows:
        try:
            row_data = {}
            for t_col in template_cols:
                mapping = mapping_dict.get(t_col["id"])
                if mapping and mapping.excel_column_name and mapping.excel_column_name in row:
                    row_data[t_col["column_name"]] = json_safe_cell_value(row[mapping.excel_column_name])
            row_json = json.dumps(row_data, sort_keys=True, ensure_ascii=False)
            if row_json in seen_existing:
                existing_count += 1
            else:
                new_count += 1
                seen_existing.add(row_json)
        except Exception:
            continue
    return new_count, existing_count


def count_new_and_existing_rows(client_id: int, template_id: int, rows: list, template_cols: list, mapping_dict: dict):
    """Count rows with exact JSONB equality without transferring historical row payloads."""
    incoming_row_data = []
    for row in rows:
        try:
            row_data = {}
            for t_col in template_cols:
                mapping = mapping_dict.get(t_col["id"])
                if mapping and mapping.excel_column_name and mapping.excel_column_name in row:
                    row_data[t_col["column_name"]] = json_safe_cell_value(row[mapping.excel_column_name])
            incoming_row_data.append(row_data)
        except Exception:
            # Preserve the old helper's behavior: rows that cannot be serialized
            # are left for the main validation loop to report.
            continue

    if not incoming_row_data:
        return 0, 0

    try:
        incoming_json = Json(
            incoming_row_data,
            dumps=lambda value: json.dumps(value, default=str, ensure_ascii=False),
        )
        result = execute_query_pg(
            """
            WITH incoming AS (
                SELECT value AS row_data
                FROM jsonb_array_elements(%s::jsonb)
            ),
            existing AS (
                SELECT DISTINCT row_data
                FROM client_data
                WHERE client_id = %s AND template_id = %s AND is_deleted = FALSE
            ),
            new_distinct AS (
                SELECT DISTINCT i.row_data
                FROM incoming i
                LEFT JOIN existing e ON e.row_data = i.row_data
                WHERE e.row_data IS NULL
            )
            SELECT
                (SELECT COUNT(*) FROM new_distinct)::int AS new_count,
                ((SELECT COUNT(*) FROM incoming) -
                 (SELECT COUNT(*) FROM new_distinct))::int AS existing_count
            """,
            (incoming_json, client_id, template_id),
        )
        if result and "new_count" in result[0] and "existing_count" in result[0]:
            return int(result[0]["new_count"]), int(result[0]["existing_count"])
        raise ValueError("set-based count returned an unexpected shape")
    except Exception as error:
        logger.warning(
            "upload_validation_set_count_fallback client_id=%s template_id=%s error=%s",
            client_id,
            template_id,
            error,
        )
        return _count_new_and_existing_rows_legacy(
            client_id, template_id, rows, template_cols, mapping_dict
        )

# ---------- CHANGE DETECTION (Phase 3: document-level grouping using document_group_id) ----------
def _row_comparison_fingerprint(row_data: dict) -> tuple:
    """Return the exact normalized values used by the legacy row comparison.

    Blank/missing values are omitted because the existing comparison treats both
    as an empty string. Sorting the pairs makes row-order changes within one
    document irrelevant while retaining exact value comparison.
    """
    normalized = []
    for field, value in row_data.items():
        value_text = str(value or '').strip()
        if value_text:
            normalized.append((field, value_text))
    return tuple(sorted(normalized))


def _comparison_column_names(template_cols: list, request_mappings: list, template: dict) -> set:
    """Template metadata is authoritative; legacy request choices cannot override it."""
    conflicts = [c['column_name'] for c in template_cols if c.get('comparison_review_required')]
    if conflicts:
        raise HTTPException(409, 'Previous client comparison settings differ. Ask an administrator to review Compare for changes in Edit Template and save it. Fields: ' + ', '.join(conflicts))
    return {c['column_name'] for c in template_cols if database.resolve_column_comparison(c, template)}


def _comparison_calendar_date(value, delimiter, order, *, stored=False):
    """Resolve comparison-only dates; never guess an old ambiguous text order."""
    try:
        kind = classify_date_cell(value)
        if kind == 'native':
            return value.strftime('%Y-%m-%d')
        if kind == 'excel_serial':
            return excel_serial_to_datetime(value).strftime('%Y-%m-%d')
        if kind != 'text':
            return None
        if not stored:
            return parse_date_flexible(value, delimiter, order).strftime('%Y-%m-%d')
        # Old uploads need not use today's picker settings. The primary Date
        # uses its stored extract_date; other dates require an unambiguous parse.
        candidates = set()
        for separator in ('/', '-', ''):
            for component_order in ('DMY', 'MDY', 'YMD'):
                try:
                    candidates.add(parse_date_flexible(value, separator, component_order).strftime('%Y-%m-%d'))
                except (ValueError, TypeError):
                    pass
        return next(iter(candidates)) if len(candidates) == 1 else None
    except (ValueError, TypeError, OverflowError):
        return None


def _formatting_comparison_values(row_data, template_cols, delimiter, order,
                                 *, primary_date=None, stored=False, included_fields=None):
    """Comparable views only: identities, concurrency guards and raw rows stay exact."""
    values = dict(_row_comparison_fingerprint(row_data))
    if included_fields is not None:
        values = {name: value for name, value in values.items() if name in included_fields}
    date_columns = [c['column_name'] for c in template_cols if c.get('data_type') == 'Date']
    context = _ledger_context.get()
    primary_column = (context['roles']['date'] if context else
                      (date_columns[0] if date_columns else None))
    description_columns = {
        c['column_name'] for c in template_cols
        if str(c['column_name']).strip().casefold() in {
            'description', 'voucher detail', 'voucher description', 'transaction description',
            'particulars', 'narration',
        }
    }
    if context:
        description_columns.add(context['roles']['description'])
    for name in date_columns:
        if name not in values:
            continue
        day = primary_date if name == primary_column else None
        day = day or _comparison_calendar_date(row_data.get(name), delimiter, order, stored=stored)
        if day:
            values[name] = ('calendar_date', day)
    for name in description_columns & values.keys():
        # Normalize horizontal spacing and line endings, keeping wording,
        # punctuation, case and paragraph boundaries (including tags) intact.
        text = values[name].replace('\r\n', '\n').replace('\r', '\n')
        values[name] = re.sub(r'[ \t]+', ' ', text).strip()
    return values


LINE_KEY_ALIASES = {
    "line id", "line_id", "line no", "line_no", "line number",
    "line_number", "line item", "line_item", "item no", "item_no",
    "item number", "item_number",
}


def get_line_key_column(template_cols: list) -> Optional[dict]:
    """Return a configured line identifier column, when the template has one."""
    for column in template_cols:
        normalized = str(column.get("column_name") or "").strip().lower()
        if normalized in LINE_KEY_ALIASES:
            return column
    return None


def _normalized_key_value(value) -> str:
    if value is None:
        return ""
    return str(value).strip().casefold()


def _comparison_line_key(template_cols: list, row_data: dict, template_id: int) -> str:
    """Build the current-row identity without using mutable row particulars."""
    document_group_id = get_document_group_id(template_cols, row_data, template_id)
    line_column = get_line_key_column(template_cols)
    if line_column:
        line_value = _normalized_key_value(row_data.get(line_column["column_name"]))
        if not line_value:
            raise HTTPException(
                400,
                f"The template has a line identifier column ('{line_column['column_name']}'), "
                "but at least one uploaded line has it blank.",
            )
        return f"{document_group_id}|line:{line_value}"
    return document_group_id


def _date_text_for_row(row_data: dict, template_cols: list, delimiter: str, order: str) -> Optional[str]:
    """Extract one comparable ISO date from a normalized current row."""
    _amount, date_text = extract_columns_from_row(row_data, template_cols, delimiter, order)
    return date_text


def _date_text(value) -> Optional[str]:
    if value is None:
        return None
    text = str(value)
    return text[:10] if len(text) >= 10 else text


def _changed_field_names(old_row_data: dict, new_row_data: dict) -> list[str]:
    fields = sorted(set(old_row_data or {}) | set(new_row_data or {}))
    return [
        field for field in fields
        if str((old_row_data or {}).get(field) or "").strip()
        != str((new_row_data or {}).get(field) or "").strip()
    ]


def build_change_report(change_detection: dict, template_cols: list, template_id: int) -> list:
    """Return a compact report; never expose complete old/new row snapshots."""
    report = []
    for item in change_detection.get("changed", []):
        old_row = item.get("old_row_data") or {}
        new_row = item.get("new_row_data") or {}
        changed_fields = item.get("changed_fields", _changed_field_names(old_row, new_row))
        report.append({
            "action": "Changed",
            "client_data_id": item.get("client_data_id"),
            "document_id": old_row.get(resolve_document_column(template_cols)["column_name"]) if template_cols else None,
            "line_key": _comparison_line_key(template_cols, new_row, template_id),
            "changed_fields": changed_fields,
            "old_values": {
                field: old_row.get(field)
                for field in changed_fields
            },
            "new_values": {
                field: new_row.get(field)
                for field in changed_fields
            },
        })
    for row_data in change_detection.get("new", []):
        report.append({
            "action": "Added",
            "client_data_id": None,
            "document_id": row_data.get(resolve_document_column(template_cols)["column_name"]) if template_cols else None,
            "line_key": _comparison_line_key(template_cols, row_data, template_id),
            "changed_fields": [],
        })
    for item in change_detection.get("informational_updates", []):
        old_row = item.get("old_row_data") or {}
        new_row = item.get("new_row_data") or {}
        fields = sorted(item.get("field_updates", {}))
        report.append({
            "action": "Informational update",
            "client_data_id": item.get("client_data_id"),
            "document_id": old_row.get(resolve_document_column(template_cols)["column_name"]) if template_cols else None,
            "line_key": _comparison_line_key(template_cols, new_row, template_id),
            "changed_fields": fields,
            "old_values": {field: old_row.get(field) for field in fields},
            "new_values": {field: new_row.get(field) for field in fields},
        })
    for item in change_detection.get("missing", []):
        old_row = item.get("old_row_data") or {}
        report.append({
            "action": "Removed",
            "client_data_id": item.get("client_data_id"),
            "document_id": old_row.get(resolve_document_column(template_cols)["column_name"]) if template_cols else None,
            "line_key": _comparison_line_key(template_cols, old_row, template_id),
            "changed_fields": [],
        })
    return report


def public_change_detection(change_detection: dict, template_cols: list, template_id: int) -> dict:
    """Strip raw row payloads before a comparison result leaves the backend."""
    observations = change_detection.get('voucher_observations', [])
    period = change_detection.get('date_range') or {}
    moves = [{'voucher_id': item['voucher_id'], 'previous_dates': item['previous_dates'] or ['Unknown'],
              'incoming_dates': item.get('incoming_dates', [])} for item in observations
             if item.get('incoming_rows') and period.get('start') and
             (not item.get('previous_dates') or any(not period['start'] <= day <= period['end'] for day in item['previous_dates']))]
    return {
        'changed_voucher_count': sum(item['status'] == 'Changed' for item in observations),
        'removed_voucher_count': sum(item['status'] == 'Removed' for item in observations),
        'date_moves': moves,
        "unchanged_count": change_detection.get("unchanged_count", 0),
        "informational_update_count": len(change_detection.get("informational_updates", [])),
        "changed_count": change_detection.get("changed_count", 0),
        "new_count": change_detection.get("new_count", 0),
        "missing_count": change_detection.get("missing_count", 0),
        "total_existing_before_import": change_detection.get("total_existing_before_import", 0),
        "comparison_mode": change_detection.get("comparison_mode"),
        "date_range": change_detection.get("date_range"),
        "duplicate_group_count": change_detection.get("duplicate_group_count", 0),
        "duplicate_row_count": change_detection.get("duplicate_row_count", 0),
        "duplicate_skipped_count": change_detection.get("duplicate_skipped_count", 0),
        "duplicate_row_numbers": change_detection.get("duplicate_row_numbers", []),
        "duplicate_skipped_row_numbers": change_detection.get("duplicate_skipped_row_numbers", []),
        "report": build_change_report(change_detection, template_cols, template_id),
    }


def _response_timestamp(value):
    if value is None:
        return None
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


def _build_upload_summary(
    file_info: dict,
    template: dict,
    request: "ImportRequest",
    change_detection: dict,
    total_rows: int,
    status: Optional[str] = None,
) -> dict:
    """Build metadata/counts only; detailed action rows remain download-only."""
    date_range = change_detection.get("date_range") or {}
    return {
        "upload_id": request.file_id,
        "file_name": file_info.get("file_name"),
        "upload_date": _response_timestamp(file_info.get("upload_date")),
        "uploaded_by": file_info.get("uploaded_by") or "Analyst",
        "client_id": request.client_id,
        "template_id": request.template_id,
        "template_name": template.get("template_name"),
        "comparison_mode": change_detection.get("comparison_mode") or request.comparison_mode,
        "date_range": date_range,
        "status": status or file_info.get("status"),
        "total_rows": total_rows,
        "added_count": change_detection.get("new_count", 0),
        "changed_count": change_detection.get("changed_count", 0),
        "removed_count": change_detection.get("missing_count", 0),
        "unchanged_count": change_detection.get("unchanged_count", 0),
        "informational_update_count": change_detection.get("informational_update_count", 0),
        "duplicate_group_count": change_detection.get("duplicate_group_count", 0),
        "duplicate_row_count": change_detection.get("duplicate_row_count", 0),
        "duplicate_skipped_count": change_detection.get("duplicate_skipped_count", 0),
    }


def _persist_upload_summary(
    request: "ImportRequest",
    change_detection: dict,
    total_rows: int,
    status: Optional[str] = None,
):
    """Persist one small summary on uploaded_files; never persist the detailed report."""
    date_range = change_detection.get("date_range") or {}
    assignments = [
        "client_id = %s",
        "template_id = %s",
        "comparison_mode = %s",
        "date_range_start = %s",
        "date_range_end = %s",
        "summary_total_count = %s",
        "summary_added_count = %s",
        "summary_changed_count = %s",
        "summary_removed_count = %s",
        "summary_unchanged_count = %s",
        "summary_updated_at = CURRENT_TIMESTAMP",
    ]
    params = [
        request.client_id,
        request.template_id,
        change_detection.get("comparison_mode") or request.comparison_mode,
        date_range.get("start"),
        date_range.get("end"),
        total_rows,
        change_detection.get("new_count", 0),
        change_detection.get("changed_count", 0),
        change_detection.get("missing_count", 0),
        change_detection.get("unchanged_count", 0),
    ]
    if status is not None:
        assignments.insert(2, "status = %s")
        params.insert(2, status)
    execute_write_pg(
        f"UPDATE uploaded_files SET {', '.join(assignments)} WHERE id = %s",
        (*params, request.file_id),
    )


def _is_duplicate_upload_noop(prior_same_file: Optional[dict], change_detection: dict) -> bool:
    """Return True only when the same source file has no canonical delta to apply."""
    if not prior_same_file:
        return False
    return not any(
        change_detection.get(key, 0)
        for key in ("new_count", "changed_count", "missing_count", "informational_update_count", "source_order_refresh_required")
    )


def _assert_upload_outcome_matches_plan(
    inserted, updated, deleted, planned_new, planned_changed, planned_deleted,
    informational_updated=0, planned_informational=0,
):
    """Do not report success if the committed actions differ from the reviewed plan."""
    if ((inserted, updated, deleted, informational_updated)
            != (planned_new, planned_changed, planned_deleted, planned_informational)):
        raise HTTPException(
            500,
            "Import stopped because the saved row counts did not match the validated review. "
            "No changes were committed; validate the file again.",
        )

def _row_fingerprint_token(row_data: dict) -> str:
    """Stable compact guard for one current Client Data row."""
    payload = json.dumps(
        _row_comparison_fingerprint(row_data or {}),
        ensure_ascii=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _canonical_source_identity(
    template_cols: list,
    row_data: dict,
    template_id: int,
    occurrence_index: int = 1,
) -> str:
    """Return a compact identity for the one canonical client_data row.

    A configured line identifier gives a stable identity across corrections and
    reordering. Templates without one fall back to the row fingerprint because
    their document key alone cannot distinguish multiple lines in one document.
    """
    line_key = _comparison_line_key(template_cols, row_data, template_id)
    if not get_line_key_column(template_cols):
        line_key = f"{line_key}|fingerprint:{_row_fingerprint_token(row_data)}"
        if occurrence_index > 1:
            line_key = f"{line_key}|occurrence:{occurrence_index}"
    return hashlib.sha256(line_key.encode("utf-8")).hexdigest()


def _canonical_source_identities(
    template_cols: list,
    row_datas: list,
    template_id: int,
) -> list[str]:
    """Return stable identities for rows, including repeated no-line-key rows."""
    occurrences = {}
    identities = []
    for row_data in row_datas:
        document_group_id = get_document_group_id(template_cols, row_data, template_id)
        fingerprint = _row_fingerprint_token(row_data)
        occurrence_key = (document_group_id, fingerprint)
        occurrences[occurrence_key] = occurrences.get(occurrence_key, 0) + 1
        identities.append(
            _canonical_source_identity(
                template_cols,
                row_data,
                template_id,
                occurrence_index=occurrences[occurrence_key],
            )
        )
    return identities


def _validation_file_sha256(file_path: str) -> str:
    digest = hashlib.sha256()
    with open(file_path, "rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _ensure_uploaded_file_hash(file_id: int, file_path: str, current_hash: str = None) -> Optional[str]:
    """Persist a compact source-file hash when the stored path is available."""
    file_path = _require_staged_upload(file_path)
    if current_hash:
        return current_hash
    file_hash = _validation_file_sha256(file_path)
    execute_write_pg(
        "UPDATE uploaded_files SET file_sha256 = %s WHERE id = %s AND file_sha256 IS DISTINCT FROM %s",
        (file_hash, file_id, file_hash),
    )
    return file_hash


def _find_prior_import_with_same_hash(
    file_id: int,
    client_id: int,
    template_id: int,
    file_hash: Optional[str],
) -> Optional[dict]:
    """Find a prior imported source file with identical bytes for audit messaging."""
    if not file_hash:
        return None
    context = _ledger_context.get()
    scope = ""
    params = (file_id, client_id, template_id, file_hash)
    if context:
        scope = " AND ledger_account_code = %s AND ledger_period_start = %s AND ledger_period_end = %s"
        params += (context["account_code"], context["start"], context["end"])
    rows = execute_query_pg(
        f"""
        SELECT id, file_name, upload_date{', ledger_evidence' if context else ''}
        FROM uploaded_files
        WHERE id <> %s
          AND client_id = %s
          AND template_id = %s
          AND status = 'Imported'
          AND file_sha256 = %s
          {scope}
        ORDER BY id DESC
        LIMIT 1
        """,
        params,
    )
    return rows[0] if rows else None


def _validation_request_signature(request: "ImportRequest") -> str:
    """Hash the user-selected validation inputs, excluding the plan itself."""
    dump = getattr(request, "model_dump", None)
    payload = (
        dump(exclude={"validation_plan_id", "skip_validation"}, exclude_none=False)
        if dump
        else request.dict(
            exclude={"validation_plan_id", "skip_validation"},
            exclude_none=False,
        )
    )
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _build_validation_plan_payload(
    change_detection: dict,
    template_cols: list,
    template_id: int,
    total_rows: int,
    rejected_rows: list,
    force_included_count: int,
    rate_source: Optional[str],
    date_delimiter: str,
    date_order: str,
) -> dict:
    """Keep only actionable lines plus optimistic-concurrency guards."""
    changed = []
    for item in change_detection.get("changed", []):
        old_row = item.get("old_row_data") or {}
        new_row = item.get("new_row_data") or {}
        changed.append({
            "client_data_id": item["client_data_id"],
            "new_row_data": new_row,
            "changed_fields": item.get("changed_fields", _changed_field_names(old_row, new_row)),
            "expected_old_fingerprint": _row_fingerprint_token(old_row),
            "expected_old_version": item.get("old_version", 1),
            "verification_detail_id": item.get("verification_detail_id"),
            "document_group_id": get_document_group_id(template_cols, new_row, template_id),
            "line_key": _comparison_line_key(template_cols, new_row, template_id),
            "source_identity": item.get("source_identity") or _canonical_source_identity(
                template_cols, old_row, template_id
            ),
            "source_row_number": item.get("source_row_number"),
        })

    new_rows = []
    new_source_identities = change_detection.get("new_source_identities", [])
    if len(new_source_identities) != len(change_detection.get("new", [])):
        new_source_identities = _canonical_source_identities(
            template_cols,
            change_detection.get("new", []),
            template_id,
        )
    for index, (row_data, source_identity) in enumerate(zip(
        change_detection.get("new", []), new_source_identities
    )):
        new_rows.append({
            "row_data": row_data,
            "document_group_id": get_document_group_id(template_cols, row_data, template_id),
            "line_key": _comparison_line_key(template_cols, row_data, template_id),
            "source_identity": source_identity,
            "source_row_number": (change_detection.get('new_source_row_numbers') or [None] * len(new_source_identities))[index],
        })

    missing = []
    for item in change_detection.get("missing", []):
        old_row = item.get("old_row_data") or {}
        missing.append({
            "client_data_id": item["client_data_id"],
            "expected_old_fingerprint": _row_fingerprint_token(old_row),
            "expected_old_version": item.get("old_version", 1),
            "verification_detail_id": item.get("verification_detail_id"),
            "document_group_id": get_document_group_id(template_cols, old_row, template_id),
            "line_key": _comparison_line_key(template_cols, old_row, template_id),
        })

    history_estimated_bytes = _history_storage_estimate(change_detection)

    payload = {
        "total_rows": total_rows,
        "rejected_rows": [
            {"row_number": item.get("row_number"), "reasons": item.get("reasons", [])}
            for item in rejected_rows
        ],
        "force_included_count": force_included_count,
        "comparison_mode": change_detection.get("comparison_mode"),
        "comparison_fields": change_detection.get("comparison_fields", []),
        "date_range": change_detection.get("date_range"),
        "rate_source": rate_source,
        "date_delimiter": date_delimiter,
        "date_order": date_order,
        "unchanged_count": change_detection.get("unchanged_count", 0),
        "informational_update_count": len(change_detection.get("informational_updates", [])),
        "changed_count": len(changed),
        "new_count": len(new_rows),
        "missing_count": len(missing),
        "history_estimated_bytes": history_estimated_bytes,
        "duplicate_group_count": change_detection.get("duplicate_group_count", 0),
        "duplicate_row_count": change_detection.get("duplicate_row_count", 0),
        "duplicate_skipped_count": change_detection.get("duplicate_skipped_count", 0),
        "duplicate_row_numbers": change_detection.get("duplicate_row_numbers", []),
        "duplicate_skipped_row_numbers": change_detection.get("duplicate_skipped_row_numbers", []),
        "changed": changed,
        "informational_updates": [
            {
                "client_data_id": item["client_data_id"],
                "field_updates": item.get("field_updates", {}),
                "old_field_values": {field: (item.get("old_row_data") or {}).get(field)
                                     for field in item.get("field_updates", {})},
                "expected_old_fingerprint": item["expected_old_fingerprint"],
                "expected_old_version": item.get("expected_old_version", 1),
                "source_identity": item.get("source_identity"),
                "source_row_number": item.get("source_row_number"),
            }
            for item in change_detection.get("informational_updates", [])
        ],
        "new": new_rows,
        "missing": missing,
        "source_order": change_detection.get("source_order", []),
        "source_order_refresh_required": change_detection.get("source_order_refresh_required", False),
    }
    context = _ledger_context.get()
    # JSON plans stringify native Excel dates in DD/MM/YYYY. Retain the
    # validated day so Import never reparses it using a text-date picker.
    for kind, key in (("new", "row_data"), ("changed", "new_row_data")):
        for item in payload[kind]:
            item['validated_date'] = _date_text_for_row(item[key], template_cols, date_delimiter, date_order)
            if context:
                item['ledger_date'] = item['validated_date']
    if context:
        payload["ledger"] = {key: context[key] for key in ("account_code", "start", "end", "configuration", "scope_fingerprint", "evidence")}
    if change_detection.get('lifecycle'):
        payload['lifecycle'] = change_detection['lifecycle']
        payload['voucher_observations'] = change_detection.get('voucher_observations', [])
        # Export notes as an additional evidence column without changing shared
        # template definitions or the template/configuration freshness guard.
        payload['template_columns'] = (template_cols + [{'column_name': LEDGER_SOURCE_NOTES, 'data_type': 'Text'}]
                                       if context else template_cols)
        payload['template_fingerprint'] = digest([template_cols, rate_source])
    return payload


def _create_validation_plan(
    request: "ImportRequest",
    file_path: str,
    plan_payload: dict,
) -> str:
    plan_id = uuid4().hex
    file_hash = _validation_file_sha256(file_path)
    request_signature = _validation_request_signature(request)
    # Include the action payload, source/index growth and allocation overhead.
    with pg_transaction():
        execute_query_pg("SELECT pg_advisory_xact_lock(hashtext('upload-storage-budget'))")
        execute_query_pg("SELECT pg_advisory_xact_lock(hashtext(%s))",
                         (f"upload-validation:{request.client_id}:{request.template_id}",))
        _check_upload_storage_budget(
            len(json.dumps(plan_payload, default=str).encode("utf-8")) * 4
            + int(plan_payload.get("history_estimated_bytes", 0))
            + 1_048_576
        )
        execute_write_pg(
            """DELETE FROM upload_validation_plans
               WHERE (client_id = %s AND template_id = %s)
                  OR expires_at < now() OR status IN ('applied', 'expired', 'superseded')""",
            (request.client_id, request.template_id),
        )
        execute_write_pg(
            """
            INSERT INTO upload_validation_plans
                (plan_id, file_id, client_id, template_id, file_sha256,
                 request_signature, plan_json, status, expires_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, 'ready',
                    now() + INTERVAL '30 minutes')
            """,
            (
                plan_id, request.file_id, request.client_id, request.template_id,
                file_hash, request_signature,
                Json(plan_payload, dumps=lambda value: json.dumps(value, default=str)),
            ),
        )
    return plan_id


def _load_validation_plan(request: "ImportRequest", file_path: str) -> dict:
    rows = execute_query_pg(
        """
        SELECT plan_id, file_id, client_id, template_id, file_sha256,
               request_signature, plan_json, status, expires_at
        FROM upload_validation_plans
        WHERE plan_id = %s
        """,
        (request.validation_plan_id,),
    )
    if not rows:
        raise HTTPException(409, "Validation plan not found. Validate the file again.")
    plan = rows[0]
    if plan["status"] != "ready":
        raise HTTPException(409, "This validation plan is no longer usable. Validate the file again.")
    if plan["expires_at"] and plan["expires_at"] < datetime.now(plan["expires_at"].tzinfo):
        execute_write_pg(
            "UPDATE upload_validation_plans SET status = 'expired' WHERE plan_id = %s",
            (request.validation_plan_id,),
        )
        raise HTTPException(409, "Validation plan expired. Validate the file again.")
    if (
        plan["file_id"] != request.file_id
        or plan["client_id"] != request.client_id
        or plan["template_id"] != request.template_id
        or plan["request_signature"] != _validation_request_signature(request)
    ):
        raise HTTPException(409, "Upload settings changed after validation. Validate the file again.")
    if _validation_file_sha256(file_path) != plan["file_sha256"]:
        raise HTTPException(409, "The upload file changed after validation. Validate the file again.")
    return plan


def _legacy_compare_transactions(client_id: int, template_id: int, template_cols: list,
                         incoming_rows: list, col_mapping: dict, mapping_dict: dict,
                         rate_source: Optional[str] = None, rate_precision: int = 0) -> dict:
    """
    Compares incoming transaction rows with existing data for the same client+template.
    Groups rows by document_group_id (derived from the first column / Document ID).
    If ANY row within a document has changed, the ENTIRE document is treated as changed.

    Returns a dict with counts AND per-document data for the import loop to act upon.
    """
    result = {
        "unchanged_count": 0,
        "changed_count": 0,
        "new_count": 0,
        "missing_count": 0,
        "total_existing_before_import": 0,
        # Detailed lists consumed by the import loop:
        "unchanged": [],      # list of existing client_data_ids to skip
        "changed": [],        # list of {client_data_id, old_row_data, new_row_data, old_validation_status}
        "new": [],            # list of new row_data dicts to insert
        "missing": []         # list of {client_data_id, old_row_data} to flag as 'deleted'
    }

    # Fetch existing client_data rows for this client+template, with their document_group_id and validation status
    existing_rows = execute_query_pg(
        """
        SELECT cd.id, cd.row_data, cd.version, cd.upload_id AS source_upload_id, cd.document_group_id,
               vd.validation_status, vd.id AS verification_detail_id
        FROM client_data cd
        LEFT JOIN verification_details vd ON vd.client_data_id = cd.id
        WHERE cd.client_id = %s AND cd.template_id = %s AND cd.is_deleted = FALSE AND cd.change_detection_status != 'deleted'
        """,
        (client_id, template_id)
    )
    result["total_existing_before_import"] = len(existing_rows)

    # Group existing rows by document_group_id
    # existing_docs: doc_group_id -> { client_data_ids, row_datas, validation_statuses, ... }
    existing_docs = {}
    for row in existing_rows:
        try:
            row_data = json.loads(row['row_data']) if isinstance(row['row_data'], str) else row['row_data']
        except Exception:
            continue
        doc_group_id = row.get('document_group_id') or f"legacy-{row['id']}"
        if doc_group_id not in existing_docs:
            existing_docs[doc_group_id] = {
                "client_data_ids": [],
                "row_datas": [],
                "versions": [],
                "validation_statuses": [],
                "verification_detail_ids": []
            }
        existing_docs[doc_group_id]["client_data_ids"].append(row['id'])
        existing_docs[doc_group_id]["row_datas"].append(row_data)
        existing_docs[doc_group_id]["versions"].append(row.get('version', 1))
        existing_docs[doc_group_id]["validation_statuses"].append(row.get("validation_status") or "Pending")
        existing_docs[doc_group_id]["verification_detail_ids"].append(row.get("verification_detail_id"))

    # Build incoming row data with their document_group_id
    incoming_docs = {}  # doc_group_id -> list of row_data dicts
    for raw_row in incoming_rows:
        row_data = {}
        for t_col_name, excel_col in col_mapping.items():
            if excel_col in raw_row:
                row_data[t_col_name] = json_safe_cell_value(raw_row[excel_col], preserve_native_dates=True)

        # Rate derivation (Phase 7 of the Rate-derivation requirement) -- must run
        # here, at first assembly, so the diff below and the eventual insert/update
        # both see the same finished row_data (see apply_rate_derivation()'s docstring).
        apply_rate_derivation(row_data, template_cols, rate_source, rate_precision)
        # Restores the template's real column order -- apply_rate_derivation() may have
        # just appended 'Rate' at the end of the dict (see reorder_row_data_by_template()'s
        # docstring).
        row_data = reorder_row_data_by_template(row_data, template_cols)

        # Compute document_group_id for this incoming row
        doc_group_id = get_document_group_id(template_cols, row_data, template_id)
        if doc_group_id not in incoming_docs:
            incoming_docs[doc_group_id] = []
        incoming_docs[doc_group_id].append(row_data)

    # Track which doc_group_ids appear in the incoming data
    seen_doc_ids = set()

    for doc_group_id, incoming_rows_data in incoming_docs.items():
        seen_doc_ids.add(doc_group_id)

        if doc_group_id in existing_docs:
            existing = existing_docs[doc_group_id]
            existing_row_datas = existing["row_datas"]

            # Compare ALL rows of this document
            # If incoming has different number of rows OR any field differs → changed
            is_same = len(incoming_rows_data) == len(existing_row_datas)
            if is_same:
                incoming_fingerprints = sorted(
                    _row_comparison_fingerprint(row) for row in incoming_rows_data
                )
                existing_fingerprints = sorted(
                    _row_comparison_fingerprint(row) for row in existing_row_datas
                )
                is_same = incoming_fingerprints == existing_fingerprints

            if is_same:
                # Unchanged: preserve validation status – skip all rows in this document
                result["unchanged_count"] += len(existing["client_data_ids"])
                result["unchanged"].extend(existing["client_data_ids"])
            else:
                # Changed: overwrite ALL rows of this document
                # Pair incoming rows with existing rows 1:1 (or handle mismatch)
                for i, new_row_data in enumerate(incoming_rows_data):
                    new_row_data_for_import = new_row_data
                    if i < len(existing["client_data_ids"]):
                        result["changed"].append({
                            "client_data_id": existing["client_data_ids"][i],
                            "old_row_data": existing["row_datas"][i],
                            "new_row_data": new_row_data_for_import,
                            "old_validation_status": existing["validation_statuses"][i],
                            "old_version": existing["versions"][i],
                            "verification_detail_id": existing["verification_detail_ids"][i]
                        })
                        result["changed_count"] += 1
                    else:
                        # More incoming rows than existing – insert extras as new
                        result["new"].append(new_row_data_for_import)
                        result["new_count"] += 1
        else:
            # New document – all rows are new
            for new_row_data in incoming_rows_data:
                result["new"].append(new_row_data)
                result["new_count"] += 1

    # Missing: existing doc_group_ids not present in incoming data
    for doc_group_id, existing in existing_docs.items():
        if doc_group_id not in seen_doc_ids:
            for i, cid in enumerate(existing["client_data_ids"]):
                result["missing"].append({
                    "client_data_id": cid,
                    "old_row_data": existing["row_datas"][i],
                    "validation_status": existing["validation_statuses"][i],
                    "verification_detail_id": existing["verification_detail_ids"][i]
                })
                result["missing_count"] += 1

    return result


def compare_transactions(client_id: int, template_id: int, template_cols: list,
                         incoming_rows: list, col_mapping: dict, mapping_dict: dict,
                         rate_source: Optional[str] = None, rate_precision: int = 0,
                         comparison_mode: str = "correction",
                         date_delimiter: str = DEFAULT_DATE_DELIMITER,
                         date_order: str = DEFAULT_DATE_ORDER,
                         coverage_start: Optional[str] = None,
                         coverage_end: Optional[str] = None,
                         included_fields: Optional[set] = None) -> dict:
    """Compare exact lines first, then classify unmatched lines within each document."""
    if comparison_mode not in {"correction", "replace_range"}:
        raise HTTPException(400, "Invalid upload comparison mode")
    context = _ledger_context.get()
    coverage = None
    if comparison_mode == 'replace_range':
        coverage = declared_coverage(coverage_start or (context or {}).get('start'),
                                     coverage_end or (context or {}).get('end'))
    scope_context = {'client_id': client_id, 'template_id': template_id,
                     'account_code': (context or {}).get('account_code'),
                     'start': (coverage or context or {}).get('start'),
                     'end': (coverage or context or {}).get('end')}

    result = {
        "unchanged_count": 0,
        "changed_count": 0,
        "new_count": 0,
        "missing_count": 0,
        "total_existing_before_import": 0,
        "unchanged": [],
        "informational_updates": [],
        "informational_update_count": 0,
        "changed": [],
        "new": [],
        "new_source_identities": [],
        "missing": [],
        "comparison_mode": comparison_mode,
        "date_range": None,
    }

    incoming_records = []
    incoming_dates = []
    incoming_groups = {}
    incoming_occurrences = {}
    for source_index, raw_row in enumerate(incoming_rows, 2):
        row_data = _canonical_import_row_data(raw_row, col_mapping, template_cols, rate_source, rate_precision)
        document_group_id = get_document_group_id(template_cols, row_data, template_id)
        date_text = _date_text_for_row(row_data, template_cols, date_delimiter, date_order)
        if coverage and (not date_text or not coverage['start'] <= date_text <= coverage['end']):
            raise HTTPException(400, 'Every transaction must have a valid date inside the declared complete-report coverage.')
        if date_text:
            incoming_dates.append(date_text)
        occurrence_fingerprint = _row_comparison_fingerprint(row_data)
        if included_fields is not None:
            occurrence_fingerprint = tuple(item for item in occurrence_fingerprint if item[0] in included_fields)
        occurrence_key = (document_group_id, occurrence_fingerprint)
        incoming_occurrences[occurrence_key] = incoming_occurrences.get(occurrence_key, 0) + 1
        record = {
            "row_data": row_data,
            "source_row_number": _ledger_row_number(raw_row, source_index),
            "date_text": date_text,
            "document_group_id": document_group_id,
            "source_identity": _canonical_source_identity(
                template_cols,
                row_data,
                template_id,
                occurrence_index=incoming_occurrences[occurrence_key],
            ),
        }
        incoming_records.append(record)
        incoming_groups.setdefault(document_group_id, []).append(record)

    if coverage:
        if not any(column.get('data_type') == 'Date' for column in template_cols):
            raise HTTPException(400, 'Complete reports require a mapped Date column.')
        result['date_range'] = coverage
        # Only these same document IDs may follow a corrected date outside the
        # declared period. Other vouchers outside it remain untouched.
        if not context:
            scope_context['document_groups'] = sorted(incoming_groups)
    result['voucher_observations'] = []

    # Fetch bounded wire payloads in one read-only snapshot. A failed read must
    # finish or fail completely before comparison can produce import actions.
    scope = ""
    params = (client_id, template_id)
    if context:
        scope = (" AND EXISTS (SELECT 1 FROM uploaded_files lu WHERE lu.id = cd.upload_id AND lu.ledger_account_code = %s)"
                 " AND (cd.extract_date BETWEEN %s::date AND %s::date")
        params += (context["account_code"], context["start"], context["end"])
    elif coverage:
        scope = ' AND (cd.extract_date BETWEEN %s::date AND %s::date'
        params += (coverage['start'], coverage['end'])
    if context and coverage and incoming_groups:
        # Ledger opening/closing evidence belongs to its declared account
        # period; moving old ledger coverage needs a wider ledger export.
        boundary = execute_query_pg('''SELECT cd.id, cd.extract_date FROM client_data cd
            WHERE cd.client_id = %s AND cd.template_id = %s AND cd.is_deleted = FALSE
              AND cd.document_group_id = ANY(%s)
              AND (cd.extract_date IS NULL OR cd.extract_date < %s::date OR cd.extract_date > %s::date)
              AND EXISTS (SELECT 1 FROM uploaded_files lu WHERE lu.id = cd.upload_id AND lu.ledger_account_code = %s)''',
            (client_id, template_id, sorted(incoming_groups), coverage['start'], coverage['end'], context['account_code']))
        if any(not row.get('extract_date') or not coverage['start'] <= str(row['extract_date'])[:10] <= coverage['end'] for row in boundary):
            raise HTTPException(409, 'A voucher has lines outside this period. Include the full voucher in a wider report.')
    if context or coverage:
        if coverage and incoming_groups and not context:
            scope += ' OR cd.document_group_id = ANY(%s)'
            params += (sorted(incoming_groups),)
        scope += ')'

    existing_rows = execute_query_pg(
        f"""
        SELECT cd.id, cd.row_data, cd.version, cd.document_group_id,
               cd.source_identity, cd.source_row_number, cd.source_order_upload_id,
               cd.extract_date, vd.validation_status, vd.id AS verification_detail_id,
               {SOURCE_ROW_HASH_SQL} AS scope_row_hash
        FROM client_data cd
        LEFT JOIN LATERAL (
            SELECT id, validation_status
            FROM verification_details
            WHERE client_data_id = cd.id
            ORDER BY id DESC
            LIMIT 1
        ) vd ON TRUE
        WHERE cd.client_id = %s AND cd.template_id = %s AND cd.is_deleted = FALSE
          {scope}
        ORDER BY cd.id
        """,
        params,
        page_size=UPLOAD_COMPARISON_PAGE_SIZE,
    )
    result["total_existing_before_import"] = len(existing_rows)

    existing_groups = {}
    for row in existing_rows:
        try:
            row_data = json.loads(row["row_data"]) if isinstance(row["row_data"], str) else row["row_data"]
        except Exception:
            continue
        document_group_id = row.get("document_group_id") or get_document_group_id(template_cols, row_data, template_id)
        existing_groups.setdefault(document_group_id, []).append({
            "client_data_id": row["id"],
            "old_row_data": row_data,
            "old_version": row.get("version", 1),
            "old_validation_status": row.get("validation_status") or "Pending",
            "verification_detail_id": row.get("verification_detail_id"),
            "source_identity": row.get("source_identity"),
            "source_upload_id": row.get("source_upload_id"),
            "source_row_number": row.get("source_row_number"),
            "source_order_upload_id": row.get("source_order_upload_id"),
            "comparison_date": _date_text(row.get("extract_date")),
            "extract_date": _date_text(row.get("extract_date")) or _date_text_for_row(
                row_data, template_cols, date_delimiter, date_order
            ),
        })

    line_column = get_line_key_column(template_cols)
    all_document_ids = sorted(set(existing_groups) | set(incoming_groups))
    for document_group_id in all_document_ids:
        changed_before = result['changed_count']
        incoming = list(incoming_groups.get(document_group_id, []))
        existing = list(existing_groups.get(document_group_id, []))
        unmatched_incoming = []
        unmatched_existing = list(existing)
        new_values = {id(r): _formatting_comparison_values(
            r['row_data'], template_cols, date_delimiter, date_order, primary_date=r['date_text'],
            included_fields=included_fields,
        ) for r in incoming}
        old_values = {id(r): _formatting_comparison_values(
            r['old_row_data'], template_cols, date_delimiter, date_order,
            primary_date=r['comparison_date'], stored=True, included_fields=included_fields
        ) for r in existing}

        def changed_item(old, new):
            before, after = old_values[id(old)], new_values[id(new)]
            return {
                "client_data_id": old["client_data_id"],
                "old_row_data": old["old_row_data"],
                "new_row_data": new["row_data"],
                "old_validation_status": old["old_validation_status"],
                "old_version": old["old_version"],
                "verification_detail_id": old["verification_detail_id"],
                "source_identity": old.get("source_identity"),
                "source_upload_id": old.get("source_upload_id"),
                "source_row_number": new['source_row_number'],
                "changed_fields": sorted(k for k in before.keys() | after.keys()
                                         if before.get(k, '') != after.get(k, '')),
            }

        def ambiguous_correction(uploaded, current, account=None):
            voucher = (incoming or [{}])[0].get('row_data', {}).get(
                resolve_document_column(template_cols)['column_name'], document_group_id) if template_cols else document_group_id
            # Compare field populations, not every possible row pairing. A
            # matching amount population must not be labelled an amount change.
            from collections import Counter
            candidates = set().union(*(old_values[id(r)].keys() for r in current),
                                     *(new_values[id(r)].keys() for r in uploaded))
            fields = sorted(field for field in candidates if
                Counter(str(old_values[id(r)].get(field, '')) for r in current) !=
                Counter(str(new_values[id(r)].get(field, '')) for r in uploaded))
            account_text = f", account '{str(account)[:80]}'" if account is not None else ''
            raise HTTPException(409,
                f"Ambiguous transaction correction for voucher '{str(voucher)[:80]}'{account_text}: "
                f"{len(uploaded)} uploaded and {len(current)} existing lines cannot be paired safely. "
                f"Fields to review: {', '.join(fields[:8]) or 'line identity'}. "
                "Provide stable source Line IDs before changing repeated lines; review source differences before retrying.")

        def equivalent(record, stored):
            return tuple(sorted((old_values if stored else new_values)[id(record)].items()))

        def exact(record, stored):
            raw = record['old_row_data' if stored else 'row_data']
            # Include the resolved date when known, avoiding equal text with
            # different DMY/MDY interpretations being accepted as unchanged.
            primary = record.get('comparison_date' if stored else 'date_text')
            fingerprint = _row_comparison_fingerprint(raw)
            if included_fields is not None:
                fingerprint = tuple(item for item in fingerprint if item[0] in included_fields)
            return fingerprint, primary

        matched, unmatched_incoming, unmatched_existing = match_unchanged(incoming, existing, exact, equivalent)
        matched_existing = [old for _new, old in matched]
        result['unchanged'].extend(old['client_data_id'] for old in matched_existing)
        result['unchanged_count'] += len(matched)
        result.setdefault('source_order', []).extend(_source_order_pairs(incoming, matched_existing, exact, equivalent))
        excluded_fields = (set(column['column_name'] for column in template_cols) - included_fields
                           if included_fields is not None else set())
        for new_record, old_record in matched:
            field_updates = {
                field: new_record['row_data'].get(field)
                for field in excluded_fields
                if str(old_record['old_row_data'].get(field) or '').strip()
                != str(new_record['row_data'].get(field) or '').strip()
            }
            if field_updates:
                result['informational_updates'].append({
                    'client_data_id': old_record['client_data_id'],
                    'field_updates': field_updates,
                    'old_row_data': old_record['old_row_data'],
                    'new_row_data': new_record['row_data'],
                    'expected_old_fingerprint': _row_fingerprint_token(old_record['old_row_data']),
                    'expected_old_version': old_record.get('old_version', 1),
                    'source_identity': new_record['source_identity'],
                    'source_row_number': new_record['source_row_number'],
                })
        result['informational_update_count'] = len(result['informational_updates'])

        # Explicit line IDs are authoritative. Unmatched IDs must remain new or
        # removed lines and must never inherit another transaction's history.
        if line_column:
            remaining_incoming = list(unmatched_incoming)
            unmatched_incoming = []
            for incoming_record in remaining_incoming:
                line_value = _normalized_key_value(incoming_record["row_data"].get(line_column["column_name"]))
                match_index = next(
                    (index for index, old in enumerate(unmatched_existing)
                     if _normalized_key_value(old["old_row_data"].get(line_column["column_name"])) == line_value),
                    None,
                ) if line_value else None
                if match_index is None:
                    unmatched_incoming.append(incoming_record)
                    continue
                old = unmatched_existing.pop(match_index)
                result["changed"].append(changed_item(old, incoming_record))
                result["changed_count"] += 1

        pairs = []
        if not line_column and unmatched_incoming and unmatched_existing:
            # Pair only where the document and available row anchors make a
            # correction safe. Ambiguous corrections block correction uploads;
            # complete reports retain unpaired lines as explicit add/remove.
            account_column = next((column["column_name"] for column in template_cols
                                   if str(column["column_name"]).strip().casefold()
                                   in {"account code", "account_code", "account detail", "ledger code"}), None)
            if account_column:
                # Account and movement side can identify a unique correction;
                # sorting mutable amounts/descriptions cannot establish identity.
                def anchor(data):
                    debit = data.get("Debit", data.get("Debit Amount", 0))
                    credit = data.get("Credit", data.get("Credit Amount", 0))
                    return (_normalized_key_value(data.get(account_column)),
                            bool(float(debit or 0)), bool(float(credit or 0)))
                for new in list(unmatched_incoming):
                    key = anchor(new["row_data"])
                    incoming_matches = [item for item in unmatched_incoming if anchor(item["row_data"]) == key]
                    existing_matches = [item for item in unmatched_existing if anchor(item["old_row_data"]) == key]
                    if len(incoming_matches) == len(existing_matches) == 1:
                        old = existing_matches[0]
                        pairs.append((old, new))
                        unmatched_existing.remove(old)
                        unmatched_incoming.remove(new)
                    elif existing_matches and not coverage:
                        ambiguous_correction(incoming_matches, existing_matches, new['row_data'].get(account_column))
            elif len(unmatched_incoming) == len(unmatched_existing) == 1:
                pairs.append((unmatched_existing.pop(), unmatched_incoming.pop()))
            elif not coverage:
                ambiguous_correction(unmatched_incoming, unmatched_existing)
        for old, new in pairs:
            result["changed"].append(changed_item(old, new))
            result["changed_count"] += 1
        for new in unmatched_incoming:
            result["new"].append(new["row_data"])
            result.setdefault('new_source_row_numbers', []).append(new['source_row_number'])
            result["new_source_identities"].append(new["source_identity"])
            result["new_count"] += 1
        if comparison_mode == "replace_range":
            for old in unmatched_existing:
                result["missing"].append({
                    "client_data_id": old["client_data_id"],
                    "old_row_data": old["old_row_data"],
                    "validation_status": old["old_validation_status"],
                    "verification_detail_id": old["verification_detail_id"],
                    "source_upload_id": old.get("source_upload_id"),
                    "source_identity": old.get("source_identity"),
                })
                result["missing_count"] += 1

        # Archive the complete previous voucher even if one line changes;
        # unchanged occurrences retain their existing IDs and evidence links.
        if existing and ((coverage and (unmatched_incoming or unmatched_existing))
                         or result['changed_count'] > changed_before):
            from collections import Counter
            comparison_columns = [c['column_name'] for c in template_cols
                                  if included_fields is None or c['column_name'] in included_fields]
            comparison_columns += [LEDGER_SOURCE_NOTES] if context else []
            fields = sorted(name for name in comparison_columns if
                Counter(str(old_values[id(r)].get(name, '')) for r in existing) !=
                Counter(str(new_values[id(r)].get(name, '')) for r in incoming))
            result['voucher_observations'].append({
                'document_group_id': document_group_id,
                'voucher_id': (incoming[0]['row_data'] if incoming else existing[0]['old_row_data']).get(resolve_document_column(template_cols)['column_name'], document_group_id),
                'status': 'Changed' if incoming else 'Removed',
                'changed_fields': fields or ['Line composition'],
                'old_ids': [r['client_data_id'] for r in existing],
                'previous_dates': sorted({r['comparison_date'] for r in existing if r.get('comparison_date')}),
                'incoming_dates': sorted({r['date_text'] for r in incoming if r.get('date_text')}),
                'incoming_rows': [{'row_data': r['row_data'], 'extract_date': r['date_text']} for r in incoming],
            })

    observations = result['voucher_observations']
    if observations and (coverage or context):
        groups = sorted(item['document_group_id'] for item in observations)
        old_ids = {key for item in observations for key in item['old_ids']}
        members = execute_query_pg('''SELECT cd.id FROM client_data cd
            WHERE cd.client_id = %s AND cd.template_id = %s AND cd.is_deleted = FALSE
              AND cd.document_group_id = ANY(%s)'''
            + (' AND EXISTS (SELECT 1 FROM uploaded_files lu WHERE lu.id = cd.upload_id AND lu.ledger_account_code = %s)' if context else ''),
            (client_id, template_id, groups) + ((context['account_code'],) if context else ()),
            page_size=UPLOAD_COMPARISON_PAGE_SIZE)
        if {row['id'] for row in members} != old_ids:
            raise HTTPException(409, 'A previous voucher has lines outside this period. Use wider coverage to include it completely.')
        if coverage and not context:
            scope_context['document_groups'] = sorted(set(scope_context['document_groups']) | set(groups))
    result['source_order_refresh_required'] = any(
        item['source_row_number'] != item['expected_source_row_number']
        or item['expected_source_order_upload_id'] is None for item in result.get('source_order', []))
    if (coverage or result['changed'] or result['new'] or result['missing']
            or result['informational_updates'] or result['source_order_refresh_required']):
        result['lifecycle'] = {'scope': scope_context, 'source_fingerprint': source_fingerprint(existing_rows),
                               'fingerprint_method': 'postgres' if existing_rows and 'scope_row_hash' in existing_rows[0] else 'python'}
    return result


# ---------- DATA MODELS ----------
class ColumnMapping(BaseModel):
    template_column_id: int
    template_column_name: str
    excel_column_name: Optional[str] = None
    matched: bool = False
    mapping_status: str = "unmatched"
    # Accepted for older clients only; authoritative policy is stored on the template.
    compare_for_changes: Optional[bool] = None

class EditedCellValue(BaseModel):
    # A value the user typed into a blank cell in the Row Selection grid (Phase 3/4 of the
    # upload-reliability requirement). row_number matches validation_errors[].row_number
    # (header=1, first data row=2); excel_column is the raw Excel header text.
    row_number: int
    excel_column: str
    value: str

class ImportRequest(BaseModel):
    # Accepted for older clients; the server replaces this with the shared policy.
    backup_enabled: Optional[bool] = None
    template_id: int
    file_id: int
    client_id: int
    ledger_account_code: Optional[str] = None
    ledger_period_start: Optional[str] = None
    ledger_period_end: Optional[str] = None
    mappings: List[ColumnMapping]
    mode: str = "create"
    # A complete replacement may remove current rows missing from the uploaded
    # date range. A correction upload is additions/changes only and never
    # interprets an absent row as a deletion.
    comparison_mode: str = "correction"
    coverage_start: Optional[str] = None
    coverage_end: Optional[str] = None
    confirm_empty_period: bool = False
    # Server-issued, short-lived validation plan. Import must use the plan
    # produced by Validate rather than re-reading and re-comparing the workbook.
    validation_plan_id: Optional[str] = None
    # Source control choice is file-specific and never a saved column default.
    control_total_row: Optional[StrictInt] = None
    control_total_confirmed: StrictBool = False
    unique_column_id: Optional[int] = None
    skip_validation: bool = False
    sheet_name: str = "Sheet1"
    # Selected date format (Phase 4 of the date-format requirement). None means
    # "not sent" and falls back to DEFAULT_DATE_DELIMITER/DEFAULT_DATE_ORDER --
    # distinct from "" which is itself a valid delimiter (no delimiter).
    date_delimiter: Optional[str] = None
    date_order: Optional[str] = None
    # Session-only row exclusion (Phase 1 of the row-exclusion requirement).
    # Row numbers match validation_errors[].row_number (header=1, first data row=2).
    # None/absent means "exclude nothing" -- identical to today's behavior.
    excluded_rows: Optional[List[int]] = None
    # Values typed into blank cells (Phase 4 of the upload-reliability requirement).
    # None/absent means "no edits" -- identical to today's behavior (genuine blanks stay blank).
    edited_cells: Optional[List[EditedCellValue]] = None
    # Rows explicitly ticked "Import anyway" in the Exceptions -- Rejected Rows list
    # (Phase 4 of the blank-field-rejection requirement). Same row-number scheme as
    # excluded_rows/rejected_rows[].row_number (header=1, first data row=2).
    # None/absent means "force-include nothing" -- identical to today's behavior
    # (every rejected row stays excluded).
    force_include_rows: Optional[List[int]] = None

class AutoMapRequest(BaseModel):
    file_id: int
    template_id: int
    sheet_name: str = "Sheet1"

# ---------- SAVED COLUMN MAPPING MODELS (Phase A1 of 3: save column mapping feature) ----------
class SavedMappingEntry(BaseModel):
    template_column_id: int
    excel_column: str
    compare_for_changes: Optional[bool] = None

class SaveMappingRequest(BaseModel):
    client_id: int
    template_id: int
    mappings: List[SavedMappingEntry]

# ---------- DATE PREVIEW MODELS (Phase 5 of the date-format requirement) ----------
class DatePreviewMapping(BaseModel):
    template_column_name: str
    excel_column_name: str

class DatePreviewRequest(BaseModel):
    file_id: int
    sheet_name: str = "Sheet1"
    date_mappings: List[DatePreviewMapping]  # only the Date-typed columns currently mapped
    date_delimiter: Optional[str] = None
    date_order: Optional[str] = None

# ---------- RATE DERIVATION AT IMPORT TIME (Phase 7 of the Rate-derivation requirement) ----------
def get_rate_precision() -> int:
    """
    Reads the 'rate_precision' app_settings key (Phase 1/2 of the Rate-derivation
    requirement), independent of decimal_precision. Falls back to 0 if missing
    or unparseable, same defensive convention as everywhere else that reads a
    setting expected to be a small int (0/2/4).
    """
    rows = execute_query_pg("SELECT value FROM app_settings WHERE key = 'rate_precision'")
    if not rows:
        return 0
    try:
        return int(rows[0]['value'])
    except (TypeError, ValueError):
        return 0


def find_named_column(template_cols: list, name: str) -> Optional[dict]:
    """
    Case-insensitive/trimmed lookup of a template column by column_name --
    generalizes the "must be named exactly 'Rate'/'Quantity'/'Amount'" naming
    convention already established for Rate-derivation eligible templates.
    """
    target = name.strip().lower()
    for col in template_cols:
        if col['column_name'].strip().lower() == target:
            return col
    return None


def resolve_upload_rate_source(template_cols: list, col_mapping: Optional[dict]) -> str:
    """
    Resolves a template's 'auto' rate_source into a concrete 'imported'/'derived'
    for one specific upload (Phase B of the Rate Source 'auto' requirement).
    Unlike apply_rate_derivation()'s per-row work, this is a per-upload decision --
    every row in the same upload shares the same column mapping, so it's resolved
    once and reused for the whole file, never re-derived per row.

    'imported' if this upload's own column mapping (`col_mapping`: template column
    name -> Excel column name, as actually chosen on THIS upload -- not the
    template's static column list) maps a Rate column; 'derived' otherwise, i.e.
    the uploaded file simply has no Rate column mapped, so it must be computed.
    """
    mapped_cols = [c for c in template_cols if col_mapping and col_mapping.get(c['column_name'])]
    return 'imported' if find_named_column(mapped_cols, 'Rate') else 'derived'


def apply_rate_derivation(row_data: dict, template_cols: list, rate_source: Optional[str], rate_precision: int) -> None:
    """
    Populates/rounds row_data['Rate'] in place for Purchase Order/Store Receiving/
    Vendor Bill templates (rate_source is None for every other template -- a
    complete no-op here). Must be called at the point row_data is first assembled
    from the raw Excel row, before compare_transactions() diffs it against
    existing stored rows -- doing it after would make a re-upload look
    artificially "changed" forever, since a previously-imported row already has
    a Rate value baked into its stored row_data.

    rate_source here is always already resolved to 'imported'/'derived' -- a
    template's 'auto' value is resolved once per upload by
    resolve_upload_rate_source() before this function is ever called (see call
    sites), so this function never sees the literal 'auto'.

    'derived': Rate = Amount / Quantity, rounded to rate_precision. Quantity
    missing/blank/0 leaves Rate as None -- a normal "Rate unavailable" outcome,
    never an error/rejection (Rate is not one of the 3 critical fields
    document_id/amount/date checked by find_critical_blank_reasons()).
    'imported': Rate is already whatever came from the mapped Excel "Rate"
    column (or absent, if the file has no such column) -- just round it.
    """
    if rate_source is None:
        return
    rate_col = find_named_column(template_cols, 'Rate')
    if not rate_col:
        return  # this template has no Rate column to populate

    if rate_source == 'derived':
        qty_col = find_named_column(template_cols, 'Quantity')
        amount_col = find_named_column(template_cols, 'Amount')
        if not qty_col or not amount_col:
            return  # nothing to derive from
        qty_raw = row_data.get(qty_col['column_name'])
        amount_raw = row_data.get(amount_col['column_name'])
        try:
            qty = float(qty_raw) if qty_raw not in (None, '') else 0.0
        except (TypeError, ValueError):
            qty = 0.0
        if qty == 0:
            row_data['Rate'] = None
            return
        try:
            amount = float(amount_raw) if amount_raw not in (None, '') else 0.0
        except (TypeError, ValueError):
            row_data['Rate'] = None
            return
        row_data['Rate'] = round(amount / qty, rate_precision)
    else:  # 'imported'
        rate_raw = row_data.get('Rate')
        if rate_raw in (None, ''):
            return
        try:
            row_data['Rate'] = round(float(rate_raw), rate_precision)
        except (TypeError, ValueError):
            pass  # leave whatever non-numeric value was there -- not this function's concern


def reorder_row_data_by_template(row_data: dict, template_cols: list) -> dict:
    """
    Rebuilds row_data with keys in the template's configured sort_order, keeping
    only keys that are actually present. Needed because a derived field (e.g.
    Rate, which has no Excel mapping) gets appended at the end of the dict by
    apply_rate_derivation() -- called right after it, at every point row_data is
    first assembled, so the stored JSON's key order always matches the
    template's real column order regardless of when a given key was added.
    """
    return {t_col['column_name']: row_data[t_col['column_name']]
            for t_col in template_cols if t_col['column_name'] in row_data}


def _canonical_import_row_data(
    row: dict,
    col_mapping: dict,
    template_cols: list,
    rate_source: Optional[str],
    rate_precision: int,
) -> dict:
    """Build exactly the row payload that the import path stores in ``client_data``."""
    row_data = {
        template_column: json_safe_cell_value(
            row[excel_column], preserve_native_dates=True
        )
        for template_column, excel_column in col_mapping.items()
        if excel_column in row
    }
    apply_rate_derivation(row_data, template_cols, rate_source, rate_precision)
    ordered = reorder_row_data_by_template(row_data, template_cols)
    context = _ledger_context.get()
    notes = (context or {}).get('row_notes', {}).get(id(row))
    if notes:
        if any(column['column_name'].casefold() == LEDGER_SOURCE_NOTES.casefold() for column in template_cols):
            raise HTTPException(400, f"Template column '{LEDGER_SOURCE_NOTES}' conflicts with attached ledger notes.")
        # Store the original note text once, on its parent canonical row. Source
        # positions remain compact evidence and do not affect row identity.
        ordered[LEDGER_SOURCE_NOTES] = '\n'.join(notes)
    return ordered


def _deduplicate_exact_rows(
    rows: list,
    col_mapping: dict,
    template_cols: list,
    template_id: int,
    rate_source: Optional[str],
    rate_precision: int,
    row_numbers: Optional[list] = None,
) -> dict:
    """Retain every source row while reporting repeated canonical lines.

    Identical-looking ledger lines can be legitimate accounting lines. They are
    counted for visibility, but none are removed before comparison or import.
    """
    retained_rows = list(rows)
    seen = {}
    groups = []
    affected_row_numbers = []
    retained_row_numbers = []

    for position, row in enumerate(rows):
        row_number = row_numbers[position] if row_numbers else position + 2
        retained_row_numbers.append(row_number)
        row_data = _canonical_import_row_data(
            row, col_mapping, template_cols, rate_source, rate_precision
        )
        fingerprint = _row_fingerprint_token(row_data)
        occurrence = seen.get(fingerprint)
        if occurrence is None:
            seen[fingerprint] = {
                "row_numbers": [row_number],
                "row": row,
            }
            continue

        occurrence["row_numbers"].append(row_number)

    for occurrence in seen.values():
        if len(occurrence["row_numbers"]) < 2:
            continue
        groups.append(occurrence["row_numbers"])
        affected_row_numbers.extend(occurrence["row_numbers"])

    return {
        "rows": retained_rows,
        "row_numbers": retained_row_numbers,
        "duplicate_group_count": len(groups),
        "duplicate_row_count": len(affected_row_numbers),
        "duplicate_skipped_count": 0,
        "duplicate_row_numbers": sorted(affected_row_numbers),
        "duplicate_skipped_row_numbers": [],
    }


def _strict_duplicate_row_errors(
    rows: list,
    col_mapping: dict,
    template_cols: list,
    template_id: int,
    rate_source: Optional[str],
    rate_precision: int,
    row_numbers: Optional[list] = None,
) -> list:
    """Return blocking errors for repeated explicit line keys in one upload.

    Templates without a line-key column may legitimately contain repeated exact
    ledger rows, so those rows are retained and are not blocking errors.
    """
    if not get_line_key_column(template_cols):
        return []

    grouped = {}
    for position, row in enumerate(rows):
        row_number = row_numbers[position] if row_numbers else position + 2
        row_data = _canonical_import_row_data(
            row, col_mapping, template_cols, rate_source, rate_precision
        )
        source_identity = _canonical_source_identity(
            template_cols, row_data, template_id
        )
        grouped.setdefault(source_identity, []).append(
            (row_number, get_document_group_id(template_cols, row_data, template_id))
        )

    errors = []
    for source_identity, occurrences in grouped.items():
        if len(occurrences) < 2:
            continue
        row_numbers = [row_number for row_number, _ in occurrences]
        document_group_id = occurrences[0][1]
        for row_number in row_numbers:
            other_rows = [value for value in row_numbers if value != row_number]
            errors.append({
                "row_number": row_number,
                "errors": [{
                    "column": "Row-level",
                    "excel_column": None,
                    "error_type": "duplicate_row",
                    "message": (
                        "Duplicate transaction line detected in this upload; "
                        f"the same normalized line also appears on row(s) {other_rows}."
                    ),
                    "document_group_id": document_group_id,
                    "source_identity": source_identity,
                }],
            })
    return errors


def _assert_active_identity_collisions(
    client_id: int,
    template_id: int,
    source_identities: list,
    excluded_client_data_ids: list = None,
) -> None:
    """Block writes that would collide with a different active canonical row."""
    identities = list(dict.fromkeys(identity for identity in source_identities if identity))
    if not identities:
        return
    query = """
        SELECT id, source_identity
        FROM client_data
        WHERE client_id = %s
          AND template_id = %s
          AND is_deleted = FALSE
          AND change_detection_status <> 'deleted'
          AND source_identity = ANY(%s)
    """
    params = [client_id, template_id, identities]
    excluded = list(dict.fromkeys(excluded_client_data_ids or []))
    if excluded:
        query += " AND NOT (id = ANY(%s))"
        params.append(excluded)
    collisions = execute_query_pg(query, tuple(params))
    if collisions:
        raise HTTPException(
            409,
            "Import blocked: one or more rows would duplicate an existing active "
            f"canonical transaction identity ({len(collisions)} collision(s)). "
            "Review the source file before retrying.",
        )


# ---------- HELPER: Resolve configured document identity; preserve legacy templates ----------
def get_document_group_id(template_cols: list, row_data: dict, template_id: int) -> str:
    """
    Prefix the configured Document ID with the template ID (and ledger account).
    Unconfigured templates retain their existing first-column convention.
    If the column value is empty/missing, returns a fallback hash.
    """
    # Resolving a document never aggregates its source detail rows.
    try:
        first_col = resolve_document_column(template_cols)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    context = _ledger_context.get()
    prefix = f"{template_id}-ledger:{context['account_code']}:" if context else f"{template_id}-"
    if first_col:
        doc_value = row_data.get(first_col["column_name"])
        if doc_value is not None and doc_value != '':
            return f"{prefix}{str(doc_value).strip()}"
    # Fallback: use a hash of all row_data
    import hashlib
    return f"{prefix}{hashlib.md5(str(row_data).encode()).hexdigest()[:8]}"


# ---------- HELPER: Extract numeric & date columns from row data ----------
def extract_columns_from_row(row_data: dict, template_cols: list,
                              delimiter: str = DEFAULT_DATE_DELIMITER, order: str = DEFAULT_DATE_ORDER) -> tuple:
    """
    Extracts the first numeric and first date value from a row based on template column types.
    Returns (amount, date_str) where amount is float or None, date_str is ISO date or None.

    amount/date are tracked with independent found-flags -- confirmed live (2026-08-09) that a
    shared `break` here previously exited the loop the moment EITHER was found, so any template
    listing its Date column before its Amount column (i.e. nearly all of them) permanently lost
    the amount, since the Date column matches first and the loop never reaches the later Numeric
    column at all. Do not reintroduce a single shared break for both branches.

    A literal 0 in a Numeric column is also treated as "no value here" and skipped -- confirmed
    live (2026-08-09) the Transaction Report template's Debit Amount/Credit Amount columns always
    store BOTH as a real number, never blank (a receipt-type voucher has Debit Amount: 0.0), so
    without this a Debit-side 0 would permanently win over the real Credit Amount for ~62% of
    real cashbook rows. Matches the existing debit/credit-side convention already established by
    is_cashbook_leg_row() (routers/verification.py), which treats a 0 the same way.

    A column literally named "Amount" (case-insensitive/trimmed, via find_named_column()) is
    preferred over the generic "first Numeric column" rule below when one exists -- confirmed
    with the user, 2026-08-09. Purchase Order/Store Receiving/Vendor Bill templates all have
    Quantity as a Numeric column before Amount in sort_order, and GET /api/data/preview's
    amount_min/amount_max filter reads extract_amount directly, so "first numeric" would
    silently resolve to Quantity there. A blank/zero named-Amount value falls through to the
    generic ordered search below, same as any other unresolved Numeric column.
    """
    context = _ledger_context.get()
    if context:
        roles = context["roles"]
        debit = _ledger_decimal(row_data.get(roles["debit"]))
        credit = _ledger_decimal(row_data.get(roles["credit"]))
        movement = None if debit is None and credit is None else (debit or Decimal(0)) - (credit or Decimal(0))
        value = row_data.get(roles["date"])
        kind = classify_date_cell(value)
        # Native dates stringify for JSON storage. Carry the already validated
        # ISO date in temporary plan metadata instead of guessing its format again.
        planned_day = context.get("planned_dates", {}).get(id(row_data))
        if planned_day is not None:
            day = planned_day
        elif kind == 'native':
            day = value.strftime('%Y-%m-%d')
        elif kind == 'excel_serial':
            day = excel_serial_to_datetime(value).strftime('%Y-%m-%d')
        elif kind == 'text':
            day = parse_date_flexible(value, delimiter, order).strftime('%Y-%m-%d')
        else:
            day = None
        return float(movement) if movement is not None else None, day
    amount = None
    date_str = None
    amount_found = False
    date_found = False

    amount_col = find_named_column(template_cols, 'Amount')
    if amount_col and amount_col['data_type'] == 'Numeric':
        named_value = row_data.get(amount_col['column_name'])
        if named_value is not None and named_value != '':
            try:
                parsed_named_amount = float(named_value)
                if parsed_named_amount != 0:
                    amount = parsed_named_amount
                    amount_found = True
            except:
                pass

    for col in template_cols:
        if amount_found and date_found:
            break
        col_name = col['column_name']
        value = row_data.get(col_name)
        if value is None or value == '':
            continue
        if col['data_type'] == 'Numeric' and not amount_found:
            try:
                parsed_amount = float(value)
                if parsed_amount != 0:
                    amount = parsed_amount
                    amount_found = True  # take first non-zero numeric
            except:
                pass
        elif col['data_type'] == 'Date' and not date_found:
            try:
                kind = classify_date_cell(value)
                if kind == 'native':
                    date_str = value.strftime('%Y-%m-%d')
                elif kind == 'text':
                    try:
                        dt = parse_date_flexible(value, delimiter, order)
                        date_str = dt.strftime('%Y-%m-%d')
                    except ValueError:
                        pass
                elif kind == 'excel_serial':
                    try:
                        dt = excel_serial_to_datetime(value)
                        date_str = dt.strftime('%Y-%m-%d')
                    except:
                        pass
                date_found = True  # take first date
            except:
                pass
    return amount, date_str


# ---------- ROW-LEVEL CRITICAL FIELD REJECTION (Phase 2 of the blank-field-rejection requirement) ----------
def get_critical_field_columns(template_cols: list) -> dict:
    """
    Identifies which template columns fill the 3 "critical" roles used for
    row-level blank rejection -- Document ID / Amount / Date -- using the SAME
    configured/legacy conventions shared by document grouping and validation:
      - document_id = configured Document ID (first column for legacy templates)
      - amount      = the first Numeric-typed column (matches
        extract_columns_from_row()'s "take first numeric" rule)
      - date        = the first Date-typed column (matches
        extract_columns_from_row()'s "take first date" rule)
    A role is None if this template has no column filling it (e.g. no Date column).
    """
    context = _ledger_context.get()
    if context:
        by_name = {column["column_name"]: column for column in template_cols}
        return {key: by_name[context["roles"][role]] for key, role in
                (("document_id", "voucher"), ("amount", "debit"), ("date", "date"))}
    return {
        'document_id': resolve_document_column(template_cols),
        'amount': next((c for c in template_cols if c['data_type'] == 'Numeric'), None),
        'date': next((c for c in template_cols if c['data_type'] == 'Date'), None),
    }


CRITICAL_FIELD_LABELS = {
    'document_id': 'Document ID',
    'amount': 'Amount',
    'date': 'Date',
}


def find_critical_blank_reasons(row: dict, critical_cols: dict, mapping_dict: dict) -> list:
    """
    Returns a reason dict for each critical role (Document ID/Amount/Date) that is
    both mapped in this upload session and blank in this raw Excel row. A role with
    no column in this template, or whose column isn't mapped to an Excel header
    this session, is skipped -- nothing to check.

    "Blank" uses the same test as apply_edited_cell_values() (pd.isna() for
    non-strings, stripped-empty for strings) for consistency within this file.
    """
    reasons = []
    for role, t_col in critical_cols.items():
        if role == "amount" and _ledger_context.get():
            fields = _ledger_context.get()["excel_roles"]
            if all(_ledger_decimal(row.get(fields[side])) is None for side in ("debit", "credit")):
                reasons.append({"role": "amount", "column": None, "excel_column": None,
                                "message": "Both Debit and Credit are blank."})
            continue
        if not t_col:
            continue
        mapping = mapping_dict.get(t_col['id'])
        if not mapping or not mapping.excel_column_name:
            continue
        excel_col = mapping.excel_column_name
        if excel_col not in row:
            continue
        value = row[excel_col]
        is_blank = pd.isna(value) if not isinstance(value, str) else value.strip() == ''
        if is_blank:
            reasons.append({
                "role": role,
                "column": t_col['column_name'],
                "excel_column": excel_col,
                "message": f"Missing {CRITICAL_FIELD_LABELS[role]}."
            })
    return reasons


def compute_data_quality_flag(row_data: dict, critical_cols: dict) -> Optional[str]:
    """
    Returns 'incomplete' if any critical field (Document ID/Amount/Date) is blank in
    the final row_data (template column names -> values), else None.

    A blank critical field can only reach this point (insert/update time) if the row
    was rejected by find_critical_blank_reasons() and then explicitly force-included
    via the Exceptions -- Rejected Rows override (Phase 4) -- normal validation never
    lets a blank-critical-field row survive to insertion otherwise. Computing the flag
    here, from the final row_data, avoids having to thread a "force-included" marker
    through compare_transactions()'s own row_data reconstruction.
    """
    for t_col in critical_cols.values():
        if not t_col:
            continue
        value = row_data.get(t_col['column_name'])
        if value is None or value == '':
            return 'incomplete'
    return None


# ---------- EXISTING UPLOAD ENDPOINT ----------
def _read_standalone_workbook(contents):
    """Reject web exports before staging; never fetch linked worksheet resources."""
    if not contents:
        raise HTTPException(400, "File is empty. Choose an Excel workbook.")
    if contents.startswith((b'\xff\xfe', b'\xfe\xff')):
        prefix = contents[:32768].decode('utf-16', errors='replace')
    else:
        prefix = contents[:32768].decode('utf-8-sig', errors='replace')
    if prefix.lstrip().startswith('<') and re.search(r'<(?:!doctype\s+html|html|table|frameset)\b', prefix, re.I):
        if re.search(r'<(?:frameset|frame)\b', prefix, re.I):
            message = ("This file links to worksheets stored elsewhere. Open the original with its companion folder and "
                       "save as .xlsx, or export a standalone workbook from the ERP.")
        else:
            message = "HTML exports are not Excel workbooks. Export as .xlsx or .xls, then upload the workbook."
        raise HTTPException(400, message)
    try:
        return pd.read_excel(BytesIO(contents))
    except ImportError as error:
        raise HTTPException(400, "Excel reader unavailable. Contact the administrator.") from error
    except Exception as error:
        logger.warning("Excel upload could not be read (%s)", type(error).__name__)
        raise HTTPException(400, "Excel cannot read this workbook. Check for damage or a password, then save a new .xlsx copy.") from error


@router.post("/api/import-excel")
async def import_excel(file: UploadFile = File(...), session=Depends(get_current_user)):
    if not (file.filename or '').casefold().endswith(('.xlsx', '.xls')):
        raise HTTPException(400, "Choose an .xlsx or .xls file.")
    contents = await file.read()
    df = _read_standalone_workbook(contents)
    file_path = _build_upload_storage_path(file.filename)
    with open(file_path, "wb") as f:
        f.write(contents)
    file_sha256 = hashlib.sha256(contents).hexdigest()
    file_id = execute_write_pg(
        "INSERT INTO uploaded_files (file_name, file_path, file_type, record_count, status, uploaded_by, file_sha256) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id",
        (file.filename, file_path, 'excel', len(df), 'Pending', session["username"], file_sha256)
    )
    log_audit(session["username"], "Upload", "Import", f"Uploaded file: {file.filename} ({len(df)} rows)")
    return {"message": f"File '{file.filename}' uploaded successfully ({len(df)} rows)", "file_id": file_id, "record_count": len(df)}

# ---------- AUTO-MAP ENDPOINT (FIXED with Pydantic model) ----------
@router.post("/api/upload/auto-map")
def auto_map_headers(request: AutoMapRequest):
    file_id = request.file_id
    template_id = request.template_id
    sheet_name = request.sheet_name

    # Fetch template columns
    template_cols = execute_query_pg(
        """
        SELECT m.id AS column_id, m.column_name, m.data_type, p.reconciliation_role, p.compare_for_changes, p.comparison_review_required, p.sort_order
        FROM template_column_mappings p
        JOIN master_columns m ON p.column_id = m.id
        WHERE p.template_id = %s AND m.is_active = TRUE
        ORDER BY p.sort_order ASC
        """,
        (template_id,)
    )
    if not template_cols:
        raise HTTPException(400, "Template has no columns")

    file_info = execute_query_pg("SELECT file_path FROM uploaded_files WHERE id = %s", (file_id,))
    if not file_info:
        raise HTTPException(404, "File not found")

    try:
        df = pd.read_excel(file_info[0]['file_path'], sheet_name=sheet_name)
        excel_headers = df.columns.tolist()
    except Exception as e:
        # Fallback to first sheet if specified sheet doesn't exist
        try:
            df = pd.read_excel(file_info[0]['file_path'])
            excel_headers = df.columns.tolist()
        except Exception as e2:
            raise HTTPException(400, "Could not read the selected Excel sheet. Check the file and sheet, then retry.")

    mappings = []
    used = set()
    for t_col in template_cols:
        t_name_clean = t_col['column_name'].strip().lower().replace(" ", "")
        best_match = None
        best_score = 0
        for h in excel_headers:
            if h in used:
                continue
            h_clean = str(h).strip().lower().replace(" ", "")
            if h_clean == t_name_clean:
                best_match = h
                best_score = 1.0
                break
            if t_name_clean in h_clean or h_clean in t_name_clean:
                score = min(len(t_name_clean), len(h_clean)) / max(len(t_name_clean), len(h_clean))
                if score > best_score:
                    best_score = score
                    best_match = h
        if best_match is None and len(excel_headers) > len(template_cols):
            pos = template_cols.index(t_col)
            if pos < len(excel_headers) and excel_headers[pos] not in used:
                best_match = excel_headers[pos]
                best_score = 0.3

        mappings.append({
            "column_id": t_col['column_id'],
            "column_name": t_col['column_name'],
            "data_type": t_col['data_type'],
            "reconciliation_role": t_col.get('reconciliation_role'),
            "compare_for_changes": t_col.get('compare_for_changes'),
            "excel_column": best_match,
            "is_matched": best_match is not None,
            "match_score": best_score,
            "sort_order": t_col['sort_order']
        })
        if best_match:
            used.add(best_match)

    return {
        "file_id": file_id,
        "template_id": template_id,
        "excel_headers": excel_headers,
        "mappings": mappings,
        "auto_mapped_count": len([m for m in mappings if m['is_matched']]),
        "total_columns": len(mappings)
    }

# ---------- SAVE / LOAD COLUMN MAPPING (Phase A1 of 3: save column mapping feature) ----------
@router.post("/api/upload/save-mapping")
def save_column_mapping(request: SaveMappingRequest):
    """
    Saves (or overwrites) the confirmed Excel-column -> template-column mapping for a
    client+template. The replacement is atomic and uses one bulk database write.
    """
    client = execute_query_pg("SELECT id FROM clients WHERE id = %s AND is_active = TRUE", (request.client_id,))
    if not client:
        raise HTTPException(404, "Client not found or inactive")
    template = execute_query_pg("SELECT id FROM templates WHERE id = %s AND is_active = TRUE", (request.template_id,))
    if not template:
        raise HTTPException(404, "Template not found")

    entries = [
        (request.client_id, request.template_id, entry.template_column_id, entry.excel_column)
        for entry in request.mappings
        if entry.excel_column and entry.excel_column.strip()
    ]
    with pg_transaction() as conn:
        execute_write_pg(
            "DELETE FROM saved_column_mappings WHERE client_id = %s AND template_id = %s AND NOT (template_column_id = ANY(%s::int[]))",
            (request.client_id, request.template_id, [entry[2] for entry in entries])
        )
        if entries:
            execute_batch_pg(
                conn,
                """
                INSERT INTO saved_column_mappings
                    (client_id, template_id, template_column_id, excel_column)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (client_id, template_id, template_column_id) DO UPDATE
                SET excel_column = EXCLUDED.excel_column, updated_at = CURRENT_TIMESTAMP
                """,
                entries,
            )

    saved_count = len(entries)
    log_audit(
        "Analyst",
        "Save",
        "Upload",
        f"Saved column mapping for client {request.client_id}, template {request.template_id} ({saved_count} columns)"
    )
    return {"message": "Column mapping saved successfully", "saved_count": saved_count}

@router.get("/api/upload/saved-mapping")
def get_saved_column_mapping(client_id: int, template_id: int):
    """
    Returns the previously saved mapping for a client+template, if any. Returns an empty/false
    result rather than 404 when nothing is saved yet -- Phase A2's auto-apply-on-load only needs
    to check has_saved_mapping, no special-case error handling required on the frontend.
    """
    rows = execute_query_pg(
        "SELECT template_column_id, excel_column FROM saved_column_mappings WHERE client_id = %s AND template_id = %s",
        (client_id, template_id)
    )
    return {
        "has_saved_mapping": len(rows) > 0,
        "mappings": [{"template_column_id": r["template_column_id"], "excel_column": r["excel_column"]} for r in rows]
    }


@router.delete("/api/upload/saved-mapping")
def delete_saved_column_mapping(client_id: int, template_id: int):
    """
    Removes the saved mapping for a client+template (Phase A3, final phase). No client/template
    existence checks -- same leniency as the GET endpoint above, since "nothing was saved" is a
    normal outcome, not an error. After this, the next auto-apply check (applySavedMapping() in
    upload.js) sees has_saved_mapping: false and falls back to ordinary auto-mapping, identical to
    a client+template that never had a saved mapping.
    """
    existing = execute_query_pg(
        "SELECT COUNT(*) AS cnt FROM saved_column_mappings WHERE client_id = %s AND template_id = %s",
        (client_id, template_id)
    )
    deleted_count = existing[0]["cnt"] if existing else 0

    execute_write_pg(
        "DELETE FROM saved_column_mappings WHERE client_id = %s AND template_id = %s",
        (client_id, template_id)
    )

    log_audit("Analyst", "Delete", "Upload",
              f"Cleared saved column mapping for client {client_id}, template {template_id} ({deleted_count} columns)")
    return {"message": "Saved column mapping cleared", "deleted_count": deleted_count}


# ---------- CHECK DUPLICATES ----------
@router.post("/api/upload/check-duplicates")
@_with_ledger_context
def check_duplicates(request: ImportRequest):
    """
    Check if any rows already exist for the given client, template, and unique column.
    Returns a list of duplicate records with old and new values for comparison.
    """
    # Validate template, file, and client
    template = execute_query_pg("SELECT id, template_name FROM templates WHERE id = %s AND is_active = TRUE", (request.template_id,))
    if not template:
        raise HTTPException(404, "Template not found")
    file_info = execute_query_pg("SELECT file_path FROM uploaded_files WHERE id = %s", (request.file_id,))
    if not file_info:
        raise HTTPException(404, "File not found")
    client = execute_query_pg("SELECT id FROM clients WHERE id = %s AND is_active = TRUE", (request.client_id,))
    if not client:
        raise HTTPException(404, "Client not found or inactive")

    # Get template columns with their types
    template_cols = execute_query_pg(
        """
        SELECT m.id, m.column_name, m.data_type, p.reconciliation_role, p.compare_for_changes, p.comparison_review_required
        FROM template_column_mappings p
        JOIN master_columns m ON p.column_id = m.id
        WHERE p.template_id = %s AND m.is_active = TRUE
        ORDER BY p.sort_order ASC
        """,
        (request.template_id,)
    )
    if not template_cols:
        raise HTTPException(400, "Template has no columns")

    mapping_dict = {m.template_column_id: m for m in request.mappings}

    # Build column mapping (template column name -> Excel column name)
    col_mapping = {}
    for t_col in template_cols:
        mapping = mapping_dict.get(t_col['id'])
        if mapping and mapping.excel_column_name:
            col_mapping[t_col['column_name']] = mapping.excel_column_name

    # Read Excel data
    try:
        df = pd.read_excel(file_info[0]['file_path'], sheet_name=request.sheet_name)
    except Exception as e:
        raise HTTPException(400, "Could not read the selected Excel sheet. Check the file and sheet, then retry.")

    rows, source_diagnostics = _prepare_source_rows(df, request, template_cols, col_mapping)

    # Build row data for each row
    row_data_list = []
    for row in rows:
        row_data = {}
        for t_col_name, excel_col in col_mapping.items():
            if excel_col in row:
                value = row[excel_col]
                if pd.isna(value):
                    value = None
                if isinstance(value, datetime):
                    value = value.strftime('%d/%m/%Y')
                row_data[t_col_name] = value
        row_data_list.append(row_data)

    # Configured document roles govern this legacy advisory endpoint as well as
    # the full Validate/import comparison. Preserve the old selector for legacy templates.
    configured_key = resolve_document_column(template_cols)
    configured_key = configured_key if configured_key and configured_key.get('reconciliation_role') == 'document_id' else None
    if not configured_key and not request.unique_column_id:
        return {"has_duplicates": False, "duplicates": []}

    # Get the unique column name
    col_id_to_name = {col['id']: col['column_name'] for col in template_cols}
    unique_col_name = configured_key['column_name'] if configured_key else col_id_to_name.get(request.unique_column_id)
    if not unique_col_name:
        return {"has_duplicates": False, "duplicates": []}

    # Check for duplicates
    duplicates = []
    for row_data in row_data_list:
        unique_value = row_data.get(unique_col_name)
        if unique_value is None or unique_value == '':
            continue

                # Case‑insensitive matching – compare lowercased values. json_extract's
                # '$."col"' path -> the JSONB ->> text-extraction operator.
        predicate = ('BTRIM(row_data->>%s) = %s' if configured_key
                     else 'LOWER(row_data->>%s) = LOWER(%s)')
        existing = execute_query_pg(
            f"""
            SELECT id, row_data, extract_amount, extract_date
            FROM client_data
            WHERE client_id = %s AND template_id = %s AND is_deleted = FALSE
            AND {predicate}
            ORDER BY id
            """,
            (request.client_id, request.template_id, unique_col_name,
             str(unique_value).strip() if configured_key else str(unique_value))
        )

        if existing:
            # row_data is JSONB - already a native dict, no json.loads() needed.
            old_data = existing[0]['row_data'] or {}
            # Get the column names from the template
            col_names = [col['column_name'] for col in template_cols]
            duplicates.append({
                "unique_value": unique_value,
                "old_data": old_data,
                # Keep every candidate line; the first-row compatibility display
                # is advisory and never decides which rows Validate/import preserves.
                "existing_rows": existing,
                "new_data": row_data,
                "columns": col_names  # <-- ADD THIS: all column names for the frontend
            })

    return {
        "has_duplicates": len(duplicates) > 0,
        "duplicates": duplicates,
        "count": len(duplicates)
    }
# ---------- DATE FORMAT LIVE PREVIEW (Phase 5 of the date-format requirement) ----------
@router.post("/api/upload/date-preview")
def preview_date_format(request: DatePreviewRequest):
    """
    Returns up to 3 sample values per mapped Date column, parsed under the
    currently selected delimiter/order, so the user can sanity-check their
    format choice before running full validation/import. Read-only -- no
    DB writes. Reuses classify_date_cell()/parse_date_flexible() exactly as
    validate_only()/validate_and_import() do, so the preview can never
    disagree with what those endpoints will actually do.
    """
    file_info = execute_query_pg("SELECT file_path FROM uploaded_files WHERE id = %s", (request.file_id,))
    if not file_info:
        raise HTTPException(404, "File not found")

    if not request.date_mappings:
        return {"samples": []}

    effective_delimiter = request.date_delimiter if request.date_delimiter is not None else DEFAULT_DATE_DELIMITER
    effective_order = request.date_order if request.date_order else DEFAULT_DATE_ORDER

    try:
        df = pd.read_excel(file_info[0]['file_path'], sheet_name=request.sheet_name)
    except Exception as e:
        raise HTTPException(400, "Could not read the selected Excel sheet. Check the file and sheet, then retry.")

    samples = []
    for mapping in request.date_mappings:
        excel_col = mapping.excel_column_name
        if excel_col not in df.columns:
            continue
        # First 3 non-empty values in this column.
        col_values = [v for v in df[excel_col].tolist() if not (isinstance(v, float) and pd.isna(v)) and v is not None][:3]
        for value in col_values:
            kind = classify_date_cell(value)
            if kind == 'native':
                samples.append({
                    "column_name": mapping.template_column_name,
                    "raw": str(value),
                    "parsed": value.strftime('%d %B %Y'),
                    "valid": True
                })
            elif kind == 'excel_serial':
                try:
                    dt = excel_serial_to_datetime(value)
                    samples.append({
                        "column_name": mapping.template_column_name,
                        "raw": str(value),
                        "parsed": dt.strftime('%d %B %Y'),
                        "valid": True
                    })
                except Exception as e:
                    samples.append({
                        "column_name": mapping.template_column_name,
                        "raw": str(value),
                        "parsed": None,
                        "valid": False,
                        "error": str(e)
                    })
            elif kind == 'text':
                try:
                    dt = parse_date_flexible(value, effective_delimiter, effective_order)
                    samples.append({
                        "column_name": mapping.template_column_name,
                        "raw": str(value),
                        "parsed": dt.strftime('%d %B %Y'),
                        "valid": True
                    })
                except ValueError as e:
                    samples.append({
                        "column_name": mapping.template_column_name,
                        "raw": str(value),
                        "parsed": None,
                        "valid": False,
                        "error": str(e)
                    })
            # 'empty' cells produce no sample -- nothing meaningful to preview.

    return {"samples": samples}


# ---------- VALIDATE ONLY (NO IMPORT) ----------
@router.get('/api/upload/backup-policy')
def upload_backup_policy(session=Depends(get_current_user)):
    folder_id = os.environ.get('DRIVE_ARCHIVE_FOLDER_ID', '').strip()
    drive_names = ('DRIVE_CLIENT_ID', 'DRIVE_CLIENT_SECRET', 'DRIVE_REFRESH_TOKEN', 'DRIVE_ARCHIVE_FOLDER_ID')
    drive_ready = all(os.environ.get(name, '').strip() for name in drive_names)
    folder_url = ('https://drive.google.com/drive/folders/' + folder_id
                  if re.fullmatch(r'[A-Za-z0-9_-]+', folder_id) else None)
    return {'allow_without_backup': unbacked_uploads_allowed(execute_query_pg),
            'archive_folder_url': folder_url, 'drive_connection_ready': drive_ready}


@router.post("/api/upload/validate")
@_log_upload_timing("validate")
@_with_ledger_context
def validate_only(request: ImportRequest, perf: bool = False):
    """
    Validate the Excel file without importing any data.
    Returns row-by-row validation errors.
    """
    # Validate template and file
    template = execute_query_pg("SELECT id, template_name, rate_source, data_purpose, ledger_columns FROM templates WHERE id = %s AND is_active = TRUE", (request.template_id,))
    if not template:
        raise HTTPException(404, "Template not found")
    file_info = execute_query_pg(
        "SELECT file_path, file_name, upload_date, uploaded_by, status, file_sha256 "
        "FROM uploaded_files WHERE id = %s",
        (request.file_id,),
    )
    if not file_info:
        raise HTTPException(404, "File not found")

    # Validate client exists
    client = execute_query_pg("SELECT id FROM clients WHERE id = %s AND is_active = TRUE", (request.client_id,))
    if not client:
        raise HTTPException(404, "Client not found or inactive")

    file_info[0]['file_path'] = _require_staged_upload(file_info[0].get('file_path'))
    file_sha256 = _ensure_uploaded_file_hash(
        request.file_id,
        file_info[0].get("file_path"),
        file_info[0].get("file_sha256"),
    )
    prior_same_file = _find_prior_import_with_same_hash(
        request.file_id, request.client_id, request.template_id, file_sha256
    )

    # Resolve the effective date format: use what the frontend sent, falling back
    # to the module defaults if absent (delimiter checks None since "" is itself valid).
    effective_delimiter = request.date_delimiter if request.date_delimiter is not None else DEFAULT_DATE_DELIMITER
    effective_order = request.date_order if request.date_order else DEFAULT_DATE_ORDER

    # Get template columns with their types
    template_cols = execute_query_pg(
        """
        SELECT m.id, m.column_name, m.data_type, p.reconciliation_role, p.compare_for_changes, p.comparison_review_required
        FROM template_column_mappings p
        JOIN master_columns m ON p.column_id = m.id
        WHERE p.template_id = %s AND m.is_active = TRUE
        ORDER BY p.sort_order ASC
        """,
        (request.template_id,)
    )
    if not template_cols:
        raise HTTPException(400, "Template has no columns")

    included_fields = _comparison_column_names(template_cols, request.mappings, template[0])

    mapping_dict = {m.template_column_id: m for m in request.mappings}

    # Build column mapping
    col_mapping = {}
    for t_col in template_cols:
        mapping = mapping_dict.get(t_col['id'])
        if mapping and mapping.excel_column_name:
            col_mapping[t_col['column_name']] = mapping.excel_column_name

    # Pre-import Rate mode visibility (Phase D of the Rate-derivation requirement) --
    # resolves the same 'auto' -> 'imported'/'derived' decision validate_and_import()
    # makes at commit time, via the same resolve_upload_rate_source() helper, so the
    # frontend can show it before the user ever clicks Upload. template_rate_source is
    # already NULL for every non-eligible template (enforced by resolve_rate_source()
    # in routers/templates.py), so rate_mode stays None there with no extra check needed.
    template_rate_source = template[0].get('rate_source')
    rate_mode = None
    if template_rate_source:
        effective_source = template_rate_source
        if effective_source == 'auto':
            effective_source = resolve_upload_rate_source(template_cols, col_mapping)
        label = {
            'auto': f"Auto → {effective_source.capitalize()}",
            'imported': 'Forced Imported',
            'derived': 'Forced Derived',
        }[template_rate_source]
        rate_mode = {
            "template_setting": template_rate_source,
            "effective_source": effective_source,
            "label": label,
        }

    # Read Excel data
    try:
        df = pd.read_excel(file_info[0]['file_path'], sheet_name=request.sheet_name)
    except Exception:
        raise HTTPException(400, "Could not read the selected Excel sheet. Check the file and sheet, then retry.")

    input_errors = _workbook_input_errors(file_info[0]['file_path'], request, template_cols, col_mapping, df)
    if input_errors:
        return _source_input_failure(input_errors, len(df))
    rows, source_diagnostics = _prepare_source_rows(df, request, template_cols, col_mapping)
    # Apply any typed blank-cell values before exclusion filtering (see
    # apply_edited_cell_values()'s docstring for why the ordering matters).
    rows = _prepare_ledger_rows(rows, request)
    source_controls = _source_reconciliation(df, rows, request, template_cols, col_mapping)
    if source_controls['status'] == 'mismatch':
        return _reconciliation_failure(source_controls, len(rows))
    rows = apply_edited_cell_values(rows, request.edited_cells)
    # Drop user-excluded rows before anything else sees them (validation, counts).
    if request.comparison_mode == 'replace_range':
        if request.force_include_rows or request.edited_cells:
            raise HTTPException(400, 'Correct complete-report errors in the source workbook; rejected rows cannot be overridden.')
        missing_mappings = [c['column_name'] for c in template_cols if not col_mapping.get(c['column_name'])
                            and not (c['column_name'].casefold() == 'rate' and rate_mode and rate_mode['effective_source'] == 'derived')]
        if missing_mappings:
            raise HTTPException(400, 'Map every complete-report column: ' + ', '.join(missing_mappings))
        for number, row in enumerate(rows, 2):
            if _ledger_row_number(row, number) in set(request.excluded_rows or []):
                raise HTTPException(400, f"Complete reports cannot exclude row {_ledger_row_number(row, number)}. Fix it or use a partial extract.")
    rows = filter_excluded_rows(rows, request.excluded_rows)

    # Which columns fill the 3 "critical" roles (Document ID/Amount/Date) for this
    # template -- computed once, reused for every row (Phase 2 of the
    # blank-field-rejection requirement).
    critical_cols = get_critical_field_columns(template_cols)

        # --- Validation (same logic as import but without inserting) ---
    validation_errors = []
    rejected_rows = []
    for row_idx, row in enumerate(rows, start=2):  # row 1 = headers, row 2 = first data
        row_idx = _ledger_row_number(row, row_idx)
        row_errors = []
        row_reasons = []
        row_data_full = {}
        try:
            # Build a full row data dict (template column names -> values)
            for t_col in template_cols:
                mapping = mapping_dict.get(t_col['id'])
                if mapping and mapping.excel_column_name:
                    excel_col = mapping.excel_column_name
                    if excel_col in row:
                        row_data_full[t_col['column_name']] = json_safe_cell_value(row[excel_col])
                    else:
                        row_data_full[t_col['column_name']] = None
            # Now check each column for errors
            for t_col in template_cols:
                mapping = mapping_dict.get(t_col['id'])
                if not mapping or not mapping.excel_column_name:
                    continue
                excel_col = mapping.excel_column_name
                if excel_col not in row:
                    continue
                value = row[excel_col]

                # A blank value is only a problem for the 3 critical fields
                # (handled separately below via find_critical_blank_reasons) --
                # a blank in any other column imports fine, nothing to check here.
                is_blank = pd.isna(value) if not isinstance(value, str) else value.strip() == ''
                if is_blank:
                    continue

                # Data type checks (non-blank values only)
                if _ledger_context.get() and t_col["column_name"] in {
                    _ledger_context.get()["roles"][role] for role in ("debit", "credit", "balance")
                }:
                    _ledger_decimal(value, balance=t_col["column_name"] == _ledger_context.get()["roles"]["balance"])
                elif t_col['data_type'] == "Numeric":
                    try:
                        float(value)
                    except:
                        row_errors.append({
                            "column": t_col['column_name'],
                            "excel_column": excel_col,
                            "error_type": "data_type",
                "message": "Enter a number."
                        })
                elif t_col['data_type'] == "Date":
                    kind = classify_date_cell(value)
                    if kind == 'native':
                        pass  # genuine Excel date cell -- already correct, nothing to validate
                    elif kind == 'excel_serial':
                        pass  # Excel serial date – will be handled on import, skip validation here
                    elif kind == 'text':
                        try:
                            parse_date_flexible(value, effective_delimiter, effective_order)
                        except (ValueError, TypeError):
                            row_errors.append({
                                "column": t_col['column_name'],
                                "excel_column": excel_col,
                                "error_type": "data_type",
                                "message": "Enter a valid date in the selected format."
                            })

            # Row-level rejection: blank Document ID/Amount/Date (Phase 2 of the
            # blank-field-rejection requirement) -- distinct from the generic
            # data-type errors above, reported separately, does not add to row_errors.
            row_reasons = find_critical_blank_reasons(row, critical_cols, mapping_dict)
        except Exception:
            # A row that fails to parse for any unforeseen reason (e.g. an
            # unexpected cell type) must not crash the whole /validate request --
            # surface it as a graceful per-row error instead of a 500.
            row_errors = [{
                "column": None,
                "excel_column": None,
                "error_type": "parse_error",
                "message": "This row could not be read. Check its values."
            }]

        if row_errors:
            validation_errors.append({
                "row_number": row_idx,
                "row_data": row_data_full,  # <-- NEW: full row data
                "errors": row_errors
            })
        if row_reasons:
            rejected_rows.append({
                "row_number": row_idx,
                "row_data": row_data_full,
                "reasons": row_reasons
            })

    rate_source = template[0].get('rate_source')
    rate_precision = get_rate_precision()
    if rate_source == 'auto':
        rate_source = resolve_upload_rate_source(template_cols, col_mapping)

    force_include_set = set(request.force_include_rows or [])
    rows_to_exclude = [
        item["row_number"] for item in rejected_rows
        if item["row_number"] not in force_include_set
    ]
    comparison_rows = filter_excluded_rows(rows, rows_to_exclude)
    comparison_row_numbers = [
        _ledger_row_number(row, row_number)
        for row_number, row in enumerate(rows, 2)
        if _ledger_row_number(row, row_number) not in set(rows_to_exclude)
    ]
    deduplication = _deduplicate_exact_rows(
        comparison_rows,
        col_mapping,
        template_cols,
        request.template_id,
        rate_source,
        rate_precision,
        row_numbers=comparison_row_numbers,
    )
    unique_comparison_rows = deduplication["rows"]
    if request.comparison_mode == 'replace_range' and (validation_errors or rejected_rows):
        # Return the actual diagnostics before comparing or creating any plan.
        return {'status': 'validation_failed', 'has_errors': True, 'can_import': False,
                'error_count': len(validation_errors), 'errors': validation_errors,
                'has_rejections': bool(rejected_rows), 'rejected_count': len(rejected_rows),
                'rejected_rows': rejected_rows, 'total_rows': len(rows),
                'source_diagnostics': source_diagnostics,
                'message': 'Fix the listed rows and validate again. No data was changed.'}
    if request.comparison_mode == 'replace_range' and not unique_comparison_rows and not request.confirm_empty_period:
        raise HTTPException(400, 'This report is empty. Confirm clearing this period to continue.')
    if request.comparison_mode == 'replace_range' and unique_comparison_rows and request.confirm_empty_period:
        raise HTTPException(400, 'This report has transactions. Uncheck “Empty report” and validate again.')
    if _ledger_context.get():
        _prepare_ledger_balance_proof(request)
        source_diagnostics['warnings'].extend(_ledger_balance_warnings(_ledger_context.get()))
    change_detection = compare_transactions(
        client_id=request.client_id,
        template_id=request.template_id,
        template_cols=template_cols,
        incoming_rows=unique_comparison_rows,
        col_mapping=col_mapping,
        mapping_dict=mapping_dict,
        rate_source=rate_source,
        rate_precision=rate_precision,
        comparison_mode=request.comparison_mode,
        date_delimiter=effective_delimiter,
        date_order=effective_order,
        coverage_start=request.coverage_start,
        coverage_end=request.coverage_end,
        included_fields=included_fields,
    )
    change_detection['comparison_fields'] = sorted(included_fields)
    source_controls = _approved_reconciliation(source_controls, unique_comparison_rows, template_cols,
                                               col_mapping, request, change_detection.get('date_range'))
    # Rate is the only numeric field this path rewrites. Avoid another full
    # canonical-row materialization for ordinary amount-controlled uploads.
    if rate_source and find_named_column(footing_columns(template_cols), 'Rate'):
        stored_controls = summarize_rows([
            _canonical_import_row_data(row, col_mapping, template_cols, rate_source, rate_precision)
            for row in unique_comparison_rows], template_cols, {c['column_name']: c['column_name'] for c in template_cols})
        if any(Decimal(value) != Decimal(source_controls['approved_detail']['footing_totals'][name])
               for name, value in stored_controls['footing_totals'].items()):
            raise HTTPException(400, 'Derived values change a Footing total. Supply the final values in Excel or review the template Footing settings.')
    change_detection.update({
        key: value
        for key, value in deduplication.items()
        if key != "rows"
    })
    public_changes = public_change_detection(change_detection, template_cols, request.template_id)
    new_count = change_detection["new_count"]
    existing_count = change_detection["unchanged_count"] + change_detection["changed_count"]
    actionable = bool(
        change_detection["new_count"]
        or change_detection["changed_count"]
        or change_detection["missing_count"]
        or change_detection.get("informational_update_count")
        or change_detection.get('source_order_refresh_required')
    )
    if _ledger_context.get() and not prior_same_file:
        actionable = True  # New coverage or balance evidence can have no transaction changes.
    duplicate_upload_noop = _is_duplicate_upload_noop(prior_same_file, change_detection)
    ledger_evidence_refresh = duplicate_upload_noop and _ledger_evidence_refresh_required(request, prior_same_file)
    if ledger_evidence_refresh:
        actionable = True
        duplicate_upload_noop = False
    duplicate_errors = []
    if not duplicate_upload_noop:
        duplicate_errors = _strict_duplicate_row_errors(
            unique_comparison_rows,
            col_mapping,
            template_cols,
            request.template_id,
            rate_source,
            rate_precision,
            row_numbers=deduplication["row_numbers"],
        )
        validation_errors.extend(duplicate_errors)
    validation_plan_id = None
    if _ledger_context.get() and rejected_rows:
        validation_errors.extend({"row_number": item["row_number"], "row_data": item["row_data"],
                                  "errors": [{"error_type": "ledger_incomplete", "message": "Correct rejected ledger rows before importing"}]}
                                 for item in rejected_rows)
    can_import = bool(actionable and not validation_errors)
    summary_status = None
    if duplicate_errors:
        summary_status = "Rejected"
    if can_import:
        plan_payload = _build_validation_plan_payload(
            change_detection,
            template_cols,
            request.template_id,
            len(rows),
            rejected_rows,
            sum(1 for row in rejected_rows if row["row_number"] in force_include_set),
            rate_source,
            effective_delimiter,
            effective_order,
        )
        plan_payload["source_diagnostics"] = source_diagnostics
        plan_payload['source_reconciliation'] = source_controls
        plan_payload['archive_timestamp'] = datetime.now(ZoneInfo('Asia/Karachi')).isoformat()
        validation_plan_id = _create_validation_plan(
            request,
            file_info[0]["file_path"],
            plan_payload,
        )
        summary_status = "Validated"
    elif not validation_errors:
        summary_status = "Ignored"

    _persist_upload_summary(
        request,
        change_detection,
        len(rows),
        status=summary_status,
    )
    upload_summary = _build_upload_summary(
        file_info[0],
        template[0],
        request,
        change_detection,
        len(rows),
        status=summary_status,
    )

    # A row can appear in both lists (e.g. a real Numeric format error in one
    # column plus a blank Date in another) -- union by row_number so it's only
    # subtracted once from valid_rows.
    bad_row_numbers = {r["row_number"] for r in validation_errors} | {r["row_number"] for r in rejected_rows}

    return {
        "status": "validation_complete",
        "source_reconciliation": source_controls,
        "backup_enabled": request.backup_enabled,
        "source_diagnostics": source_diagnostics,
        "has_errors": len(validation_errors) > 0,
        "error_count": len(validation_errors),
        "has_rejections": len(rejected_rows) > 0,
        "rejected_count": len(rejected_rows),
        "rejected_rows": rejected_rows,
        "total_rows": len(rows),
        "errors": validation_errors,
        "valid_rows": len(rows) - len(bad_row_numbers),
        "new_rows_count": new_count,
        "existing_rows_count": existing_count,
        "change_detection": public_changes,
        "can_import": can_import,
        "duplicate_upload_id": prior_same_file.get("id") if prior_same_file else None,
        "duplicate_upload_detected": bool(prior_same_file),
        "duplicate_upload_noop": duplicate_upload_noop,
        "ledger_evidence_refresh": bool(ledger_evidence_refresh),
        "duplicate_group_count": deduplication["duplicate_group_count"],
        "duplicate_row_count": deduplication["duplicate_row_count"],
        "duplicate_skipped_count": deduplication["duplicate_skipped_count"],
        "duplicate_row_numbers": deduplication["duplicate_row_numbers"],
        "duplicate_skipped_row_numbers": deduplication["duplicate_skipped_row_numbers"],
        "validation_plan_id": validation_plan_id,
        "rate_mode": rate_mode,
        "upload_summary": upload_summary,
        "ledger_evidence": _ledger_context.get()["evidence"] if _ledger_context.get() else None,
    }

DATA_AUDIT_LOG_INSERT_SQL = (
    'INSERT INTO data_audit_log (client_data_id, "user", action, old_row_data, new_row_data) '
    "VALUES (%s, %s, %s, %s, %s)"
)


def _audit_row_params(client_data_id, action, old_data=None, new_data=None, user="Analyst"):
    """
    Builds one params tuple for DATA_AUDIT_LOG_INSERT_SQL, matching
    log_data_audit_pg()'s own encoding convention exactly (used for the batched
    import write path instead of calling log_data_audit_pg() per row).
    old_row_data/new_row_data are JSONB - passed via Json(..., dumps=...) with
    the same default=str fallback json.dumps() used, rather than double
    -encoding to a JSON string ourselves.
    """
    old_data, new_data = compact_audit_payloads(action, old_data, new_data)
    return (
        client_data_id,
        user,
        action,
        Json(old_data, dumps=lambda d: json.dumps(d, default=str)) if old_data else None,
        Json(new_data, dumps=lambda d: json.dumps(d, default=str)) if new_data else None,
    )


CLIENT_DATA_HISTORY_INSERT_SQL = """
    INSERT INTO client_data_history
        (client_id, template_id, client_data_id, action, changed_by,
         source_upload_id, replacing_upload_id, source_identity, row_data,
         changed_fields, ledger_account_code, ledger_account_name)
    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
"""


def _history_row_params(request, item, action, row_data=None):
    """Retain prior rows and exact changed fields without tying them to active rows."""
    old_data = row_data if row_data is not None else item.get("old_row_data") or {}
    if isinstance(old_data, str):
        old_data = json.loads(old_data)
    changed_values = None
    if action == "changed":
        new_data = item.get("new_row_data") or {}
        if isinstance(new_data, str):
            new_data = json.loads(new_data)
        raw_diffs = {
            change["field"]: change
            for change in database.diff_row_data(old_data, new_data)
        }
        selected_fields = item.get("changed_fields")
        if selected_fields is None:
            selected_fields = sorted(raw_diffs)
        changed_values = {
            field: {
                "old_value": raw_diffs[field]["old_value"],
                "new_value": raw_diffs[field]["new_value"],
            }
            for field in selected_fields
            if field in raw_diffs
        }
    ledger = _ledger_context.get() or {}
    return (
        request.client_id,
        request.template_id,
        item["client_data_id"],
        action,
        "Analyst",
        item.get("source_upload_id", item.get("upload_id")),
        request.file_id,
        item.get("source_identity"),
        Json(old_data, dumps=lambda value: json.dumps(value, default=str)),
        Json(changed_values, dumps=lambda value: json.dumps(value, default=str))
        if changed_values is not None else None,
        ledger.get("account_code"),
        ledger.get("account_name"),
    )


def _history_storage_estimate(change_detection):
    previous_rows = [
        item.get("old_row_data") or {}
        for key in ("changed", "missing")
        for item in change_detection.get(key, [])
    ]
    changed_details = []
    for item in change_detection.get("changed", []):
        old_data = item.get("old_row_data") or {}
        new_data = item.get("new_row_data") or {}
        raw_diffs = {
            change["field"]: change
            for change in database.diff_row_data(old_data, new_data)
        }
        fields = item.get("changed_fields")
        if fields is None:
            fields = sorted(raw_diffs)
        changed_details.append({
            field: {
                "old_value": raw_diffs[field]["old_value"],
                "new_value": raw_diffs[field]["new_value"],
            }
            for field in fields if field in raw_diffs
        })
    return (
        sum(len(json.dumps(row, default=str).encode("utf-8")) + 256 for row in previous_rows)
        + sum(len(json.dumps(fields, default=str).encode("utf-8")) + 64 for fields in changed_details)
    )


# ---------- PLAN-BASED IMPORT ----------
def _guard_plan_configuration(request, payload):
    if not payload.get('lifecycle'):
        return
    templates = execute_query_pg('SELECT rate_source FROM templates WHERE id = %s AND is_active = TRUE FOR SHARE', (request.template_id,))
    columns = execute_query_pg('''SELECT m.id, m.column_name, m.data_type, p.reconciliation_role, p.compare_for_changes, p.comparison_review_required
        FROM template_column_mappings p JOIN master_columns m ON p.column_id = m.id
        WHERE p.template_id = %s AND m.is_active = TRUE ORDER BY p.sort_order FOR SHARE OF m, p''', (request.template_id,))
    if not templates:
        raise HTTPException(409, 'The upload template changed. Validate again.')
    rate = templates[0].get('rate_source')
    if rate == 'auto':
        rate = resolve_upload_rate_source(columns, {m.template_column_name: m.excel_column_name for m in request.mappings})
    if digest([columns, rate]) != payload.get('template_fingerprint'):
        raise HTTPException(409, 'Template columns or rate configuration changed. Validate again.')


def _import_validation_plan(request: ImportRequest, background_tasks: BackgroundTasks = None):
    """Apply only the actionable rows captured by Validate."""
    template = execute_query_pg(
        "SELECT id, template_name, rate_source, data_purpose, ledger_columns FROM templates WHERE id = %s AND is_active = TRUE",
        (request.template_id,),
    )
    if not template:
        raise HTTPException(404, "Template not found")
    file_info = execute_query_pg(
        "SELECT file_path, file_name, upload_date, uploaded_by, status, file_sha256 "
        "FROM uploaded_files WHERE id = %s",
        (request.file_id,),
    )
    if not file_info:
        raise HTTPException(404, "File not found")
    client = execute_query_pg(
        "SELECT id, client_name FROM clients WHERE id = %s AND is_active = TRUE",
        (request.client_id,),
    )
    if not client:
        raise HTTPException(404, "Client not found or inactive")

    completed = (get_receipt(request, execute_query_pg, _validation_request_signature(request))
                 if file_info[0].get('status') == 'Imported' else None)
    if completed:
        return completed
    plan = _load_validation_plan(request, file_info[0]["file_path"])
    plan_payload = plan["plan_json"]
    context = _ledger_context.get()
    if bool(context) != bool(plan_payload.get("ledger")):
        raise HTTPException(409, "The template purpose changed after validation; validate again")
    if context:
        context["planned_dates"] = {}
        for kind, key in (("new", "row_data"), ("changed", "new_row_data")):
            for item in plan_payload.get(kind, []):
                day = item.get("ledger_date")
                if not day or not context["start"] <= day <= context["end"]:
                    raise HTTPException(409, "Ledger transaction dates are missing from the validation plan; validate again")
                context["planned_dates"][id(item[key])] = day
    template_cols = execute_query_pg(
        """
        SELECT m.id, m.column_name, m.data_type, p.reconciliation_role, p.compare_for_changes, p.comparison_review_required
        FROM template_column_mappings p
        JOIN master_columns m ON p.column_id = m.id
        WHERE p.template_id = %s AND m.is_active = TRUE
        ORDER BY p.sort_order ASC
        """,
        (request.template_id,),
    )
    if not template_cols:
        raise HTTPException(400, "Template has no columns")

    included_fields = _comparison_column_names(template_cols, request.mappings, template[0])
    planned_comparison_fields = plan_payload.get("comparison_fields")
    source_controls = plan_payload.get('source_reconciliation') or {}
    configured_footings = sorted(c['column_name'] for c in footing_columns(template_cols))
    planned_footings = sorted(c['column'] for c in source_controls.get('checks', []))
    if configured_footings != planned_footings or (configured_footings and source_controls.get('status') != 'matched'):
        raise HTTPException(409, 'Source Footing controls are missing or changed. Validate the file again.')
    # Validation plans created before comparison settings were recorded (or
    # assembled directly by older integrations) have no authoritative field
    # list to compare. New plans always include it, so retain the stale-setting
    # guard whenever that recorded list is present.
    if planned_comparison_fields and sorted(included_fields) != sorted(planned_comparison_fields):
        raise HTTPException(409, "The column change settings changed after validation. Validate again.")

    changed_actions = plan_payload.get("changed", [])
    missing_actions = plan_payload.get("missing", [])
    source_order_actions = plan_payload.get('source_order', [])
    informational_actions = plan_payload.get('informational_updates', [])
    guarded_ids = list(dict.fromkeys(
        [item["client_data_id"] for item in changed_actions]
        + [item["client_data_id"] for item in missing_actions]
        + [item["client_data_id"] for item in informational_actions]
    ))
    current_by_id = {}
    if guarded_ids:
        current_rows = execute_query_pg(
            """
            SELECT cd.id, cd.row_data, cd.version, cd.document_group_id,
                   cd.source_identity,
                   cd.extract_date, vd.validation_status, vd.id AS verification_detail_id
            FROM client_data cd
            LEFT JOIN LATERAL (
                SELECT id, validation_status
                FROM verification_details
                WHERE client_data_id = cd.id
                ORDER BY id DESC
                LIMIT 1
            ) vd ON TRUE
            WHERE cd.id = ANY(%s)
              AND cd.client_id = %s
              AND cd.template_id = %s
              AND cd.is_deleted = FALSE
            """,
            (guarded_ids, request.client_id, request.template_id),
        )
        current_by_id = {row["id"]: row for row in current_rows}

    changed = []
    for item in changed_actions:
        current = current_by_id.get(item["client_data_id"])
        if not current:
            raise HTTPException(409, "Current data changed after validation. Validate the file again.")
        current_data = current["row_data"]
        if isinstance(current_data, str):
            current_data = json.loads(current_data)
        if (
            current.get("version", 1) != item.get("expected_old_version", 1)
            or _row_fingerprint_token(current_data) != item["expected_old_fingerprint"]
        ):
            raise HTTPException(409, "Current data changed after validation. Validate the file again.")
        changed.append({
            "client_data_id": current["id"],
            "old_row_data": current_data,
            "new_row_data": item["new_row_data"],
            "old_validation_status": current.get("validation_status") or "Pending",
            "old_version": current.get("version", 1),
            "verification_detail_id": current.get("verification_detail_id"),
            "source_identity": item.get("source_identity") or current.get("source_identity"),
            "source_row_number": item.get('source_row_number'),
            "changed_fields": item.get("changed_fields", _changed_field_names(current_data, item["new_row_data"])),
        })

    missing = []
    for item in missing_actions:
        current = current_by_id.get(item["client_data_id"])
        if not current:
            raise HTTPException(409, "Current data changed after validation. Validate the file again.")
        current_data = current["row_data"]
        if isinstance(current_data, str):
            current_data = json.loads(current_data)
        if (
            current.get("version", 1) != item.get("expected_old_version", 1)
            or _row_fingerprint_token(current_data) != item["expected_old_fingerprint"]
        ):
            raise HTTPException(409, "Current data changed after validation. Validate the file again.")
        missing.append({
            "client_data_id": current["id"],
            "old_row_data": current_data,
            "validation_status": current.get("validation_status") or "Pending",
            "verification_detail_id": current.get("verification_detail_id"),
        })

    informational_updates = []
    for item in informational_actions:
        current = current_by_id.get(item["client_data_id"])
        if not current:
            raise HTTPException(409, "Current data changed after validation. Validate the file again.")
        current_data = current["row_data"]
        if isinstance(current_data, str):
            current_data = json.loads(current_data)
        if (current.get("version", 1) != item.get("expected_old_version", 1)
                or _row_fingerprint_token(current_data) != item["expected_old_fingerprint"]):
            raise HTTPException(409, "Current data changed after validation. Validate the file again.")
        new_data = dict(current_data)
        new_data.update(item.get("field_updates", {}))
        informational_updates.append({
            "client_data_id": current["id"],
            "old_row_data": current_data,
            "new_row_data": new_data,
            "source_identity": item.get("source_identity") or _canonical_source_identity(
                template_cols, new_data, request.template_id
            ),
            "source_row_number": item.get("source_row_number"),
        })

    new_rows = [item["row_data"] for item in plan_payload.get("new", [])]
    new_row_identities = [
        item.get("source_identity") or _canonical_source_identity(
            template_cols, item["row_data"], request.template_id
        )
        for item in plan_payload.get("new", [])
    ]
    if new_rows:
        new_source_identities = [
            item.get("source_identity")
            for item in plan_payload.get("new", [])
            if item.get("source_identity")
        ]
        if new_source_identities:
            existing_new_identities = execute_query_pg(
                """
                SELECT source_identity
                FROM client_data
                WHERE client_id = %s AND template_id = %s
                  AND is_deleted = FALSE
                  AND source_identity = ANY(%s)
                  AND NOT (id = ANY(%s))
                """,
                (request.client_id, request.template_id, new_source_identities,
                 [item['client_data_id'] for item in missing + changed + informational_updates]),
            )
            if existing_new_identities:
                raise HTTPException(409, "New data was added after validation. Validate the file again.")

    planned_identities = [
        item.get("source_identity") or _canonical_source_identity(
            template_cols, item["new_row_data"], request.template_id
        )
        for item in changed
    ] + new_row_identities
    planned_identities += [item["source_identity"] for item in informational_updates]
    if len(planned_identities) != len(set(planned_identities)):
        raise HTTPException(
            409,
            "Validation plan blocked: it contains repeated canonical transaction "
            "identities. Validate the source file again after removing duplicates.",
        )
    _assert_active_identity_collisions(
        request.client_id,
        request.template_id,
        planned_identities,
        [item["client_data_id"] for item in changed + missing + informational_updates],
    )

    change_detection = {
        'voucher_observations': plan_payload.get('voucher_observations', []),
        "unchanged_count": plan_payload.get("unchanged_count", 0),
        "informational_update_count": len(informational_updates),
        "informational_updates": informational_updates,
        "changed_count": len(changed),
        "new_count": len(new_rows),
        "missing_count": len(missing),
        "duplicate_group_count": plan_payload.get("duplicate_group_count", 0),
        "duplicate_row_count": plan_payload.get("duplicate_row_count", 0),
        "duplicate_skipped_count": plan_payload.get("duplicate_skipped_count", 0),
        "duplicate_row_numbers": plan_payload.get("duplicate_row_numbers", []),
        "duplicate_skipped_row_numbers": plan_payload.get("duplicate_skipped_row_numbers", []),
        "total_existing_before_import": None,
        "unchanged": [],
        "changed": changed,
        "new": new_rows,
        "missing": missing,
        "comparison_mode": plan_payload.get("comparison_mode", request.comparison_mode),
        "date_range": plan_payload.get("date_range"),
        "source_order_refresh_required": plan_payload.get('source_order_refresh_required', False),
    }
    rate_source = plan_payload.get("rate_source")
    effective_delimiter = plan_payload.get("date_delimiter") or DEFAULT_DATE_DELIMITER
    effective_order = plan_payload.get("date_order") or DEFAULT_DATE_ORDER
    planned_dates = {
        id(item[key]): item['validated_date']
        for kind, key in (("new", "row_data"), ("changed", "new_row_data"))
        for item in plan_payload.get(kind, []) if 'validated_date' in item
    }

    def extract_planned_columns(row_data):
        amount, day = extract_columns_from_row(row_data, template_cols, effective_delimiter, effective_order)
        return amount, planned_dates.get(id(row_data), day)

    critical_cols = get_critical_field_columns(template_cols)
    rejected_rows = plan_payload.get("rejected_rows", [])
    force_included_count = plan_payload.get("force_included_count", 0)
    total_rows_attempted = plan_payload.get("total_rows", len(new_rows) + len(changed))
    cashflow_occurrence_rows = [
        reorder_row_data_by_template(row_data, template_cols)
        for row_data in new_rows
    ] + [
        reorder_row_data_by_template(item["new_row_data"], template_cols)
        for item in changed
    ]

    inserted_count = 0
    updated_count = 0
    deleted_count = 0
    replacement_cleanup = {"staged_paths": []}
    cashflow_voucher_resolution = None
    IMPORT_PROGRESS[request.file_id] = {
        "current": 0,
        "total": len(new_rows) + len(changed) + len(missing) + len(informational_updates),
        "status": "processing",
    }
    if plan_payload.get('lifecycle'):
        if file_info[0].get('status') not in ('Pending', 'Validated'):
            raise HTTPException(409, 'Use a fresh staged upload; an imported source cannot be reassigned.')
        if request.backup_enabled is not False:
            IMPORT_PROGRESS[request.file_id]['status'] = 'archiving'
    plan_payload['archive_labels'] = {'client': client[0].get('client_name') or f'Client {request.client_id}',
                                      'template': template[0]['template_name'],
                                      'source': file_info[0]['file_name'],
                                      'incoming_title': 'Incoming vouchers' if request.comparison_mode == 'replace_range' else 'Incoming extract'}
    prepared = prepare_lifecycle(request, plan_payload, execute_query_pg, execute_write_pg,
                                 pg_transaction, _validation_request_signature(request))

    with pg_transaction() as conn:
        execute_query_pg("SELECT pg_advisory_xact_lock(hashtext('upload-storage-budget'))")
        _check_upload_storage_budget(
            len(json.dumps(plan_payload, default=str).encode("utf-8")) * 4
            + int(plan_payload.get("history_estimated_bytes", 0))
            + 1_048_576
        )
        execute_query_pg(
            "SELECT pg_advisory_xact_lock(hashtext(%s))",
            (f"upload-validation:{request.client_id}:{request.template_id}",),
        )
        guard_lifecycle(prepared, execute_query_pg)
        if prepared:
            # A new validation or cleanup may supersede this plan during archive preparation.
            current_plan = execute_query_pg(
                'SELECT status, expires_at FROM upload_validation_plans WHERE plan_id = %s FOR UPDATE',
                (request.validation_plan_id,),
            )
            if (not current_plan or current_plan[0]['status'] != 'ready'
                    or (current_plan[0].get('expires_at') and current_plan[0]['expires_at'] < datetime.now(current_plan[0]['expires_at'].tzinfo))):
                raise HTTPException(409, 'Validation plan expired or was superseded while archiving. Validate again; no replacement was applied.')
        _guard_plan_configuration(request, plan_payload)
        _apply_source_order_updates(conn, request, source_order_actions)
        locked_by_id = {}
        if guarded_ids:
            locked_rows = execute_query_pg(
                """
                SELECT id, row_data, version, upload_id, source_identity
                FROM client_data
                WHERE id = ANY(%s)
                  AND client_id = %s
                  AND template_id = %s
                  AND is_deleted = FALSE
                FOR UPDATE
                """,
                (guarded_ids, request.client_id, request.template_id),
            )
            locked_by_id = {row["id"]: row for row in locked_rows}
            for item in changed_actions + missing_actions + informational_actions:
                locked = locked_by_id.get(item["client_data_id"])
                locked_data = locked.get("row_data") if locked else None
                if isinstance(locked_data, str):
                    locked_data = json.loads(locked_data)
                if (
                    not locked
                    or locked.get("version", 1) != item.get("expected_old_version", 1)
                    or _row_fingerprint_token(locked_data) != item["expected_old_fingerprint"]
                ):
                    raise HTTPException(
                        409,
                        "Current data changed after validation. Validate the file again.",
                    )
        history_params = []
        for action, actions in (("changed", changed_actions), ("deleted", missing_actions)):
            for item in actions:
                locked = locked_by_id.get(item["client_data_id"])
                if not locked:
                    raise HTTPException(409, "Current data changed after validation. Validate the file again.")
                history_item = {
                    "client_data_id": item["client_data_id"],
                    "source_upload_id": locked.get("upload_id"),
                    "source_identity": locked.get("source_identity"),
                    "new_row_data": item.get("new_row_data"),
                    "changed_fields": item.get("changed_fields"),
                }
                history_params.append(_history_row_params(
                    request, history_item, action, row_data=locked.get("row_data")
                ))
        if history_params:
            execute_batch_pg(conn, CLIENT_DATA_HISTORY_INSERT_SQL, history_params)
        _assert_active_identity_collisions(
            request.client_id,
            request.template_id,
            planned_identities,
            [item["client_data_id"] for item in changed + missing],
        )
        if context:
            _guard_ledger_plan(request, plan_payload)
            _persist_ledger_evidence(request, plan_payload["ledger"]["evidence"])
        execute_write_pg(
            "UPDATE uploaded_files SET status = 'Imported', client_id = %s WHERE id = %s",
            (request.client_id, request.file_id),
        )
        treasury_mapping = {} if context else load_treasury_mapping_postgres(conn, request.client_id)

        # Remove superseded identities before their replacement occurrences
        # are inserted; the verified archive and commit receipt share this guard.
        if missing:
            try:
                replacement_cleanup = purge_replaced_transaction_rows(
                    request.client_id, request.template_id,
                    [item['client_data_id'] for item in missing], 'Analyst', archive_proof=prepared)
                deleted_count = replacement_cleanup['deleted_count']
            except DataPurgeProtectedError as error:
                raise HTTPException(409, str(error)) from error

        changed_client_data_params = []
        changed_classification_sources = []
        changed_verification_params = []
        changed_audit_params = []
        for item in changed:
            new_row_data = item["new_row_data"]
            amount, date_str = extract_planned_columns(new_row_data)
            source_identity = item.get("source_identity") or _canonical_source_identity(
                template_cols, new_row_data, request.template_id
            )
            source_fingerprint = _row_fingerprint_token(new_row_data)
            changed_client_data_params.append((
                Json(new_row_data, dumps=lambda value: json.dumps(value, default=str)),
                request.file_id,
                get_document_group_id(template_cols, new_row_data, request.template_id),
                compute_data_quality_flag(new_row_data, critical_cols),
                rate_source,
                amount,
                date_str,
                source_identity,
                source_fingerprint,
                item.get('source_row_number'),
                request.file_id if item.get('source_row_number') else None,
                item["client_data_id"],
            ))
            changed_classification_sources.append({
                "id": item["client_data_id"],
                "client_id": request.client_id,
                "template_id": request.template_id,
                "upload_id": request.file_id,
                "row_data": new_row_data,
                "extract_date": date_str,
            })
            changed_audit_params.append(_audit_row_params(
                item["client_data_id"], "UPDATE", item["old_row_data"], new_row_data
            ))
            if item.get("verification_detail_id"):
                changed_verification_params.append((item["verification_detail_id"],))
            updated_count += 1
            IMPORT_PROGRESS[request.file_id]["current"] += 1

        if changed_client_data_params:
            execute_batch_pg(
                conn,
                """
                UPDATE client_data
                SET row_data = %s, upload_id = %s, document_group_id = %s,
                    change_detection_status = 'active', is_deleted = FALSE,
                    data_quality_flag = %s, rate_source = %s,
                    extract_amount = %s, extract_date = %s,
                    source_identity = %s, source_fingerprint = %s,
                    source_row_number = %s, source_order_upload_id = %s
                WHERE id = %s
                """,
                changed_client_data_params,
            )
        informational_client_data_params = []
        for item in informational_updates:
            new_row_data = item["new_row_data"]
            informational_client_data_params.append((
                Json(new_row_data, dumps=lambda value: json.dumps(value, default=str)),
                request.file_id,
                item["source_identity"],
                _row_fingerprint_token(new_row_data),
                item.get("source_row_number"),
                request.file_id if item.get("source_row_number") else None,
                item["client_data_id"],
            ))
        if informational_client_data_params:
            execute_batch_pg(
                conn,
                """
                UPDATE client_data
                SET row_data = %s, upload_id = %s, source_identity = %s,
                    source_fingerprint = %s, source_row_number = %s,
                    source_order_upload_id = %s
                WHERE id = %s
                """,
                informational_client_data_params,
            )
            informational_updated_count = len(informational_client_data_params)
            IMPORT_PROGRESS[request.file_id]["current"] += informational_updated_count
        else:
            informational_updated_count = 0
        if changed_verification_params:
            execute_batch_pg(
                conn,
                "UPDATE verification_details SET validation_status = 'Pending' WHERE id = %s",
                changed_verification_params,
            )
        if changed_audit_params:
            execute_batch_pg(conn, DATA_AUDIT_LOG_INSERT_SQL, changed_audit_params)
        if changed_classification_sources and not context:
            upsert_classification_rows_postgres(
                conn, changed_classification_sources, treasury_mapping, configuration_version=1
            )

        new_client_data_params = []
        new_classification_rows = []
        new_audit_params = []
        for index, (new_row_data, source_identity) in enumerate(zip(new_rows, new_row_identities)):
            amount, date_str = extract_planned_columns(new_row_data)
            source_fingerprint = _row_fingerprint_token(new_row_data)
            new_client_data_params.append((
                request.client_id,
                request.template_id,
                request.file_id,
                Json(new_row_data, dumps=lambda value: json.dumps(value, default=str)),
                amount,
                date_str,
                "Analyst",
                1,
                get_document_group_id(template_cols, new_row_data, request.template_id),
                compute_data_quality_flag(new_row_data, critical_cols),
                rate_source,
                source_identity,
                source_fingerprint,
                plan_payload['new'][index].get('source_row_number'),
                request.file_id if plan_payload['new'][index].get('source_row_number') else None,
            ))
            inserted_count += 1
            IMPORT_PROGRESS[request.file_id]["current"] += 1

        if new_client_data_params:
            new_ids = execute_batch_insert_with_ids_pg(
                conn,
                """
                INSERT INTO client_data
                    (client_id, template_id, upload_id, row_data, extract_amount,
                     extract_date, created_by, version, change_detection_status,
                     document_group_id, data_quality_flag, rate_source,
                     source_identity, source_fingerprint, source_row_number, source_order_upload_id)
                VALUES %s RETURNING id
                """,
                new_client_data_params,
                template="(%s, %s, %s, %s, %s, %s, %s, %s, 'active', %s, %s, %s, %s, %s, %s, %s)",
            )
            for row_id, new_row_data in zip(new_ids, new_rows):
                amount, date_str = extract_planned_columns(new_row_data)
                new_classification_rows.append({
                    "id": row_id,
                    "client_id": request.client_id,
                    "template_id": request.template_id,
                    "upload_id": request.file_id,
                    "row_data": new_row_data,
                    "extract_date": date_str,
                })
                new_audit_params.append(_audit_row_params(row_id, "INSERT", None, new_row_data))
            if not context:
                upsert_classification_rows_postgres(
                    conn, new_classification_rows, treasury_mapping, configuration_version=1
                )
            execute_batch_pg(conn, DATA_AUDIT_LOG_INSERT_SQL, new_audit_params)

        if missing:
            IMPORT_PROGRESS[request.file_id]["current"] += len(missing)

        if context:
            _finalize_ledger_balances(request, plan_payload['ledger']['evidence'])

        if not context:
            cashflow_voucher_resolution = resolve_upload_occurrences_postgres(
                conn,
                client_id=request.client_id,
                template_id=request.template_id,
                upload_id=request.file_id,
                row_data=cashflow_occurrence_rows,
            )
        _assert_upload_outcome_matches_plan(
            inserted_count, updated_count, deleted_count,
            len(new_rows), len(changed), len(missing),
            informational_updated_count, len(informational_updates),
        )
        execute_write_pg(
            """
            DELETE FROM upload_validation_plans
            WHERE plan_id = %s AND status = 'ready'
            """,
            (request.validation_plan_id,),
        )
        import_message = f'Successfully imported: {inserted_count} added, {updated_count} changed, {deleted_count} removed'
        if informational_updated_count:
            import_message += f', {informational_updated_count} balance-only row(s) refreshed'
        if request.backup_enabled is False:
            import_message += '. Development mode: no recovery backup was created.'
        if source_order_actions and not (inserted_count or updated_count or deleted_count):
            import_message += '. Original source row order restored; transaction values are unchanged.'
        finish_lifecycle(prepared, {
            'status': 'success', 'message': import_message,
            'total_rows': total_rows_attempted, 'imported_count': inserted_count,
            'updated_count': updated_count, 'deleted_count': deleted_count,
            'informational_updated_count': informational_updated_count,
            'backup_mode': prepared.get('archive_mode', 'drive' if prepared.get('archive') else 'not_required') if prepared else 'not_required',
            'archive_verified': bool(prepared and prepared.get('archive')),
            'change_report': prepared.get('change_report') if prepared else None,
        }, execute_write_pg)

    if replacement_cleanup["staged_paths"]:
        failures = _remove_staged_upload_files(replacement_cleanup["staged_paths"])
        if failures:
            logger.warning("Replacement staged-file cleanup requires retry (%s files)", len(failures))
    legacy_snapshot_required = (
        cashflow_voucher_resolution is None
        or not cashflow_voucher_resolution.get("eligible")
        or cashflow_voucher_resolution.get("cache_invalidation_required")
    )
    preparation_required = bool(
        cashflow_voucher_resolution
        and cashflow_voucher_resolution.get("preparation_required")
    )
    if context:
        legacy_snapshot_required = False
    elif plan_payload.get('source_order_refresh_required'):
        legacy_snapshot_required = True  # Transaction-window caches must see changed positions too.
    if legacy_snapshot_required:
        invalidate_cashflow_summary_read_models(
            request.client_id,
            reason="source_imported",
            changed_by="Analyst",
            write_fn=execute_write_pg,
        )
    preparation_job = None
    preparation_warning = None
    IMPORT_PROGRESS[request.file_id]["status"] = "preparing"
    if legacy_snapshot_required or preparation_required:
        if preparation_required:
            preparation_job, preparation_warning = _enqueue_upload_preparation(
                request, cashflow_voucher_resolution
            )
        if background_tasks is None:
            _prepare_cashflow_snapshot_background(
                request.client_id,
                request.file_id,
                request.template_id,
                cashflow_voucher_resolution,
                preparation_job["job_id"] if preparation_job else None,
            )
        else:
            background_tasks.add_task(
                _prepare_cashflow_snapshot_background,
                request.client_id,
                request.file_id,
                request.template_id,
                cashflow_voucher_resolution,
                preparation_job["job_id"] if preparation_job else None,
            )
    else:
        IMPORT_PROGRESS[request.file_id]["status"] = "complete"

    log_audit(
        "Analyst",
        "Import",
        "Upload",
        f"Imported {inserted_count} added, {updated_count} changed, {deleted_count} removed "
        f"from validation plan for client {request.client_id}, template {template[0]['template_name']}, "
        f"file ID {request.file_id}",
    )
    if background_tasks is None:
        IMPORT_PROGRESS[request.file_id]["status"] = "complete"

    _persist_upload_summary(
        request,
        change_detection,
        total_rows_attempted,
        status="Imported",
    )
    upload_summary = _build_upload_summary(
        file_info[0],
        template[0],
        request,
        change_detection,
        total_rows_attempted,
        status="Imported",
    )

    return {
        "status": "success",
        "source_diagnostics": plan_payload.get("source_diagnostics", {}),
        "source_reconciliation": source_controls,
        "archive_verified": bool(prepared and prepared.get('archive')),
        "backup_mode": prepared.get('archive_mode', 'drive' if prepared.get('archive') else 'not_required') if prepared else 'not_required',
        "change_report": prepared.get('change_report') if prepared else None,
        "message": import_message,
        "total_rows": total_rows_attempted,
        "imported_count": inserted_count,
        "updated_count": updated_count,
        "deleted_count": deleted_count,
        "informational_updated_count": informational_updated_count,
        "validation_errors": [],
        "source_order_refreshed": bool(source_order_actions),
        "rejected_rows": rejected_rows,
        "rejected_count": len(rejected_rows),
        "force_included_count": force_included_count,
        "change_detection": public_change_detection(change_detection, template_cols, request.template_id),
        "upload_summary": upload_summary,
        "cashflow_voucher_resolution": cashflow_voucher_resolution,
        "cashflow_preparation_job": preparation_job,
        "cashflow_preparation_warning": preparation_warning,
    }


# ---------- VALIDATE & IMPORT ----------
@router.post("/api/upload/import")
@_log_upload_timing("import")
@_with_ledger_context
def validate_and_import(request: ImportRequest, background_tasks: BackgroundTasks = None, perf: bool = False):
    if request.validation_plan_id:
        return _import_validation_plan(request, background_tasks)

    # Validate template and file
    template = execute_query_pg("SELECT id, template_name, rate_source, data_purpose, ledger_columns FROM templates WHERE id = %s AND is_active = TRUE", (request.template_id,))
    if not template:
        raise HTTPException(404, "Template not found")
    file_info = execute_query_pg(
        "SELECT file_path, file_name, upload_date, uploaded_by, status, file_sha256 "
        "FROM uploaded_files WHERE id = %s",
        (request.file_id,),
    )
    if not file_info:
        raise HTTPException(404, "File not found")
    # Validate client exists
    client = execute_query_pg("SELECT id FROM clients WHERE id = %s AND is_active = TRUE", (request.client_id,))
    if not client:
        raise HTTPException(404, "Client not found or inactive")

    file_info[0]['file_path'] = _require_staged_upload(file_info[0].get("file_path"))
    file_sha256 = _ensure_uploaded_file_hash(
        request.file_id,
        file_info[0].get("file_path"),
        file_info[0].get("file_sha256"),
    )
    prior_same_file = _find_prior_import_with_same_hash(
        request.file_id, request.client_id, request.template_id, file_sha256
    )

    # Resolve the effective date format: use what the frontend sent, falling back
    # to the module defaults if absent (delimiter checks None since "" is itself valid).
    effective_delimiter = request.date_delimiter if request.date_delimiter is not None else DEFAULT_DATE_DELIMITER
    effective_order = request.date_order if request.date_order else DEFAULT_DATE_ORDER

    # Get template columns with their types
    template_cols = execute_query_pg(
        """
        SELECT m.id, m.column_name, m.data_type, p.reconciliation_role, p.compare_for_changes, p.comparison_review_required
        FROM template_column_mappings p
        JOIN master_columns m ON p.column_id = m.id
        WHERE p.template_id = %s AND m.is_active = TRUE
        ORDER BY p.sort_order ASC
        """,
        (request.template_id,)
    )
    if not template_cols:
        raise HTTPException(400, "Template has no columns")

    if footing_columns(template_cols):
        raise HTTPException(400, 'Validate the Excel control totals first, then import using the validation plan.')

    included_fields = _comparison_column_names(template_cols, request.mappings, template[0])

    mapping_dict = {m.template_column_id: m for m in request.mappings}

    # Build column mapping (template column name -> Excel column name)
    col_mapping = {}
    for t_col in template_cols:
        mapping = mapping_dict.get(t_col['id'])
        if mapping and mapping.excel_column_name:
            col_mapping[t_col['column_name']] = mapping.excel_column_name

    # Read Excel data
    try:
        df = pd.read_excel(file_info[0]['file_path'], sheet_name=request.sheet_name)
    except Exception:
        raise HTTPException(400, "Could not read the selected Excel sheet. Check the file and sheet, then retry.")
    print(f"🔍 DataFrame shape: {df.shape}")  # rows, columns
    print(f"🔍 DataFrame columns: {df.columns.tolist()}")
    input_errors = _workbook_input_errors(file_info[0]['file_path'], request, template_cols, col_mapping, df)
    if input_errors:
        return _source_input_failure(input_errors, len(df))
    rows, source_diagnostics = _prepare_source_rows(df, request, template_cols, col_mapping)
    # Apply any typed blank-cell values before exclusion filtering (see
    # apply_edited_cell_values()'s docstring for why the ordering matters).
    rows = apply_edited_cell_values(rows, request.edited_cells)
    # Drop user-excluded rows before validation, import, and document_group_id
    # grouping (compare_transactions) ever see them.
    rows = filter_excluded_rows(rows, request.excluded_rows)

    # Which columns fill the 3 "critical" roles (Document ID/Amount/Date) for this
    # template -- computed once, reused for every row (Phase 2 of the
    # blank-field-rejection requirement).
    critical_cols = get_critical_field_columns(template_cols)

    # Rate derivation config (Phase 7 of the Rate-derivation requirement) --
    # rate_source is None for every template except Purchase Order/Store
    # Receiving/Vendor Bill (see resolve_rate_source() in routers/templates.py).
    rate_source = template[0].get('rate_source')
    rate_precision = get_rate_precision()
    # Phase B of the Rate Source 'auto' requirement: 'auto' is only a template-level
    # trigger, resolved once per upload (not per row -- every row in this upload
    # shares the same col_mapping) into a concrete 'imported'/'derived' before
    # anything downstream (compare_transactions, first-time-upload insert, or the
    # rate_source column stored per row) ever sees it.
    if rate_source == 'auto':
        rate_source = resolve_upload_rate_source(template_cols, col_mapping)

    # --- Validation ---
    validation_errors = []
    rejected_rows = []
    for row_idx, row in enumerate(rows, start=2):
        row_idx = _ledger_row_number(row, row_idx)
        row_errors = []
        row_reasons = []
        try:
            for t_col in template_cols:
                mapping = mapping_dict.get(t_col['id'])
                if not mapping or not mapping.excel_column_name:
                    continue
                excel_col = mapping.excel_column_name
                if excel_col not in row:
                    continue
                value = row[excel_col]

                # A blank value is only a problem for the 3 critical fields
                # (handled separately below via find_critical_blank_reasons) --
                # a blank in any other column imports fine, nothing to check here.
                is_blank = pd.isna(value) if not isinstance(value, str) else value.strip() == ''
                if is_blank:
                    continue

                if t_col['data_type'] == "Numeric":
                    try:
                        float(value)
                    except:
                        row_errors.append({
                            "column": t_col['column_name'],
                            "excel_column": excel_col,
                            "error_type": "data_type",
                            "message": "Enter a number."
                        })
                elif t_col['data_type'] == "Date":
                    kind = classify_date_cell(value)
                    if kind == 'native':
                        pass  # genuine Excel date cell -- already correct, nothing to validate
                    elif kind == 'excel_serial':
                        pass  # Excel serial date – will be handled on import, skip validation here
                    elif kind == 'text':
                        try:
                            parse_date_flexible(value, effective_delimiter, effective_order)
                        except (ValueError, TypeError):
                            row_errors.append({
                                "column": t_col['column_name'],
                                "excel_column": excel_col,
                                "error_type": "data_type",
                                "message": "Enter a valid date in the selected format."
                            })

            # Row-level rejection: blank Document ID/Amount/Date (Phase 2 of the
            # blank-field-rejection requirement) -- distinct from the generic
            # data-type errors above, reported separately, does not add to row_errors.
            row_reasons = find_critical_blank_reasons(row, critical_cols, mapping_dict)
        except Exception:
            # Same graceful-degradation guarantee as validate_only(): a row that
            # fails to parse must not crash the whole /import request.
            row_errors = [{
                "column": None,
                "excel_column": None,
                "error_type": "parse_error",
                "message": "This row could not be read. Check its values."
            }]
        if row_errors:
            validation_errors.append({"row_number": row_idx, "errors": row_errors})
        if row_reasons:
            rejected_rows.append({"row_number": row_idx, "reasons": row_reasons})

    if validation_errors:
        # Generic validation errors (bad Numeric/Date format on a non-blank value)
        # are unaffected by row-level rejection -- they still block the entire
        # import, same as before. rejected_rows is included here too, purely for
        # visibility, since a row can independently appear in both lists.
        return {
            "status": "validation_failed",
            "source_diagnostics": source_diagnostics,
            "errors": validation_errors,
            "total_rows": len(rows),
            "error_count": len(validation_errors),
            "rejected_rows": rejected_rows,
            "rejected_count": len(rejected_rows),
        }

    # Total attempted rows, captured before rejected rows are excluded below --
    # the success response's total_rows should reflect what was attempted, not
    # just what actually got inserted.
    total_rows_attempted = len(rows)

    # Exclude rows rejected for a blank critical field from insertion -- EXCEPT rows
    # the user explicitly ticked "Import anyway" for in the Exceptions -- Rejected Rows
    # list (Phase 4 of the blank-field-rejection requirement). A force-included row
    # stays in `rows` unchanged, still carrying its genuinely blank critical-field
    # cell -- no default/placeholder value is ever inserted in its place.
    force_include_set = set(request.force_include_rows or [])
    force_included_count = sum(1 for r in rejected_rows if r["row_number"] in force_include_set)
    rows_to_exclude = [r["row_number"] for r in rejected_rows if r["row_number"] not in force_include_set]
    if rows_to_exclude:
        rows = filter_excluded_rows(rows, rows_to_exclude)

    comparison_row_numbers = [_ledger_row_number(row, i) for i, row in enumerate(rows, 2)]
    deduplication = _deduplicate_exact_rows(
        rows,
        col_mapping,
        template_cols,
        request.template_id,
        rate_source,
        rate_precision,
        row_numbers=comparison_row_numbers,
    )
    rows = deduplication["rows"]

    # Complete mapped workbook rows are retained for canonical voucher resolution.
    # This must happen before change detection skips unchanged Client Data rows.
    cashflow_occurrence_rows = _cashflow_occurrence_input_rows(
        rows, col_mapping, template_cols, rate_source, rate_precision
    )

    # ---- CHANGE DETECTION (Phase 2): Compare incoming rows with existing data ----
    # Change detection always runs; it groups rows by Document ID and identifies
    # unchanged, changed, new, and missing documents.
    change_detection = compare_transactions(
        client_id=request.client_id,
        template_id=request.template_id,
        template_cols=template_cols,
        incoming_rows=rows,
        col_mapping=col_mapping,
        mapping_dict=mapping_dict,
        rate_source=rate_source,
        rate_precision=rate_precision,
        comparison_mode=request.comparison_mode,
        date_delimiter=effective_delimiter,
        date_order=effective_order,
        included_fields=included_fields,
    )
    if change_detection and change_detection.get('informational_update_count'):
        raise HTTPException(400, 'Validate this upload and use the confirmed Import button to refresh informational values.')
    if change_detection and (change_detection['changed_count'] or change_detection['missing_count']):
        raise HTTPException(400, 'Validate this correction before importing. A verified archive is required.')
    # Preserve the established first-upload path for callers that intentionally
    # disable comparison (for example, an isolated import worker or legacy
    # integration). In that mode every validated row is a new row.
    if change_detection is None:
        change_detection = {
            "unchanged": [],
            "changed": [],
            "new": rows,
            "missing": [],
            "unchanged_count": 0,
            "changed_count": 0,
            "new_count": len(rows),
            "missing_count": 0,
            "total_existing_before_import": 0,
            "comparison_mode": request.comparison_mode,
            "date_range": None,
        }
    change_detection.update({
        key: value
        for key, value in deduplication.items()
        if key != "rows"
    })

    duplicate_upload_noop = _is_duplicate_upload_noop(prior_same_file, change_detection)
    duplicate_errors = []
    if not duplicate_upload_noop:
        duplicate_errors = _strict_duplicate_row_errors(
            rows,
            col_mapping,
            template_cols,
            request.template_id,
            rate_source,
            rate_precision,
            row_numbers=deduplication["row_numbers"],
        )
    if duplicate_errors:
        _persist_upload_summary(
            request,
            {
                "comparison_mode": request.comparison_mode,
                "date_range": None,
                "new_count": 0,
                "changed_count": 0,
                "missing_count": 0,
                "unchanged_count": 0,
            },
            total_rows_attempted,
            status="Rejected",
        )
        return {
            "status": "validation_failed",
            "source_diagnostics": source_diagnostics,
            "errors": duplicate_errors,
            "total_rows": total_rows_attempted,
            "error_count": len(duplicate_errors),
            "rejected_rows": rejected_rows,
            "rejected_count": len(rejected_rows),
            "duplicate_upload_id": prior_same_file.get("id") if prior_same_file else None,
            "duplicate_upload_detected": bool(prior_same_file),
            "duplicate_upload_noop": False,
            "duplicate_group_count": deduplication["duplicate_group_count"],
            "duplicate_row_count": deduplication["duplicate_row_count"],
            "duplicate_skipped_count": deduplication["duplicate_skipped_count"],
            "duplicate_row_numbers": deduplication["duplicate_row_numbers"],
            "duplicate_skipped_row_numbers": deduplication["duplicate_skipped_row_numbers"],
        }

    pending_identities = [
        item.get("source_identity") or _canonical_source_identity(
            template_cols, item.get("new_row_data") or {}, request.template_id
        )
        for item in change_detection.get("changed", [])
    ] + (
        change_detection.get("new_source_identities", [])
        if len(change_detection.get("new_source_identities", []))
        == len(change_detection.get("new", []))
        else _canonical_source_identities(
            template_cols,
            change_detection.get("new", []),
            request.template_id,
        )
    )
    _assert_active_identity_collisions(
        request.client_id,
        request.template_id,
        pending_identities,
        [item.get("client_data_id") for item in change_detection.get("changed", [])],
    )

    public_changes = public_change_detection(change_detection, template_cols, request.template_id)
    if not (change_detection["new_count"] or change_detection["changed_count"] or change_detection["missing_count"]
            or change_detection.get('source_order_refresh_required')):
        _persist_upload_summary(request, change_detection, len(rows), status="Ignored")
        upload_summary = _build_upload_summary(
            file_info[0], template[0], request, change_detection, len(rows), status="Ignored"
        )
        IMPORT_PROGRESS[request.file_id] = {"current": 0, "total": 0, "status": "ignored"}
        log_audit(
            "Analyst",
            "Import",
            "Upload",
            f"Ignored unchanged upload file ID {request.file_id} for client {request.client_id}, template {request.template_id}",
        )
        return {
            "status": "success",
            "source_diagnostics": source_diagnostics,
            "message": (
                "Upload ignored: this source file is already imported and all "
                "validated lines are unchanged."
                if duplicate_upload_noop
                else "Upload ignored: all validated lines are unchanged."
            ),
            "ignored": True,
            "total_rows": len(rows),
            "imported_count": 0,
            "updated_count": 0,
            "deleted_count": 0,
            "validation_errors": [],
            "rejected_rows": rejected_rows,
            "rejected_count": len(rejected_rows),
            "duplicate_upload_id": prior_same_file.get("id") if prior_same_file else None,
            "duplicate_upload_detected": bool(prior_same_file),
            "duplicate_upload_noop": duplicate_upload_noop,
            "force_included_count": force_included_count,
            "change_detection": public_changes,
            "upload_summary": upload_summary,
            "cashflow_voucher_resolution": None,
            "cashflow_preparation_job": None,
        }

    # ---- IMPORT: Use change_detection result to decide per-row action ----
    inserted_count = 0
    updated_count = 0
    deleted_count = 0
    replacement_cleanup = {"staged_paths": []}

    # Row-write progress tracking (Import progress requirement, phase 1 of 3) --
    # total reflects rows about to actually be written, not the earlier validation pass.
    if change_detection is not None and change_detection.get("unchanged") is not None:
        progress_total = len(change_detection["changed"]) + len(change_detection["new"]) + len(change_detection["missing"])
    else:
        progress_total = len(rows)
    IMPORT_PROGRESS[request.file_id] = {"current": 0, "total": progress_total, "status": "processing"}

    # Reuse a single pooled connection for the whole write phase instead of
    # borrowing/returning one per row - this was 94.6% of total import time for
    # a ~4,900-row import under the original SQLite version. Each write category
    # below is batched (database.py's execute_batch_pg()/
    # execute_batch_insert_with_ids_pg(), via psycopg2.extras.execute_batch()/
    # execute_values()). The outer transaction defers the final commit until
    # both raw client_data and derived classification writes succeed.
    cashflow_voucher_resolution = None
    with pg_transaction() as conn:
        execute_query_pg("SELECT pg_advisory_xact_lock(hashtext('upload-storage-budget'))")
        execute_query_pg(
            "SELECT pg_advisory_xact_lock(hashtext(%s))",
            (f"upload-validation:{request.client_id}:{request.template_id}",),
        )
        growth_rows = change_detection.get("new", []) + [item["new_row_data"] for item in change_detection.get("changed", [])]
        _check_upload_storage_budget(
            len(json.dumps(growth_rows, default=str).encode("utf-8")) * 4
            + _history_storage_estimate(change_detection)
            + 1_048_576
        )
        _apply_source_order_updates(conn, request, change_detection.get('source_order', []))
        execute_write_pg("UPDATE uploaded_files SET status = 'Imported', client_id = %s WHERE id = %s",
                         (request.client_id, request.file_id))
        _assert_active_identity_collisions(
            request.client_id,
            request.template_id,
            pending_identities,
            [item.get("client_data_id") for item in change_detection.get("changed", [])],
        )
        treasury_mapping = load_treasury_mapping_postgres(conn, request.client_id)
        history_params = [
            _history_row_params(request, item, "changed")
            for item in change_detection.get("changed", [])
        ] + [
            _history_row_params(request, item, "deleted")
            for item in change_detection.get("missing", [])
        ]
        if history_params:
            execute_batch_pg(conn, CLIENT_DATA_HISTORY_INSERT_SQL, history_params)
        if change_detection is not None and change_detection.get("unchanged") is not None:
            # --- Re-upload scenario: handle each category ---
            # 1. Unchanged: skip — retain existing validation status (no action needed)
            #    (These rows already exist in client_data and their validation_status is preserved)

            # 2. Changed: update row_data, reset validation_status to 'Pending', log old values
            changed_client_data_params = []
            changed_classification_sources = []
            changed_verification_params = []
            changed_audit_params = []
            for item in change_detection["changed"]:
                cid = item["client_data_id"]
                new_row_data = item["new_row_data"]

                # Compute document_group_id from the new row_data
                doc_group_id = get_document_group_id(template_cols, new_row_data, request.template_id)

                # data_quality_flag (Phase 4 of the blank-field-rejection requirement) is
                # recomputed from the new row_data -- a force-included row with a still-blank
                # critical field is flagged 'incomplete', otherwise cleared back to NULL.
                data_quality_flag = compute_data_quality_flag(new_row_data, critical_cols)
                # extract_amount/extract_date (confirmed fix, 2026-08-09) are recomputed from the
                # new row_data too -- this UPDATE previously left both columns untouched on a
                # changed-row re-upload, so a row's amount/date would never refresh even after the
                # extraction bug above was fixed.
                amount, date_str = extract_columns_from_row(new_row_data, template_cols, effective_delimiter, effective_order)
                source_identity = item.get("source_identity") or _canonical_source_identity(
                    template_cols, new_row_data, request.template_id
                )
                source_fingerprint = _row_fingerprint_token(new_row_data)
                changed_client_data_params.append(
                    (Json(new_row_data, dumps=lambda d: json.dumps(d, default=str)), request.file_id, doc_group_id, data_quality_flag, rate_source, amount, date_str, source_identity, source_fingerprint, item.get('source_row_number'), request.file_id if item.get('source_row_number') else None, cid)
                )
                changed_classification_sources.append({
                    "id": cid,
                    "client_id": request.client_id,
                    "template_id": request.template_id,
                    "upload_id": request.file_id,
                    "row_data": new_row_data,
                    "extract_date": date_str,
                })
                changed_audit_params.append(_audit_row_params(cid, "UPDATE", item["old_row_data"], new_row_data))
                if item.get("verification_detail_id"):
                    changed_verification_params.append((item["verification_detail_id"],))

                updated_count += 1
                IMPORT_PROGRESS[request.file_id]["current"] += 1

            if changed_client_data_params:
                execute_batch_pg(
                    conn,
                    """
                    UPDATE client_data
                    SET row_data = %s, upload_id = %s, document_group_id = %s, change_detection_status = 'active', is_deleted = FALSE, data_quality_flag = %s, rate_source = %s, extract_amount = %s, extract_date = %s, source_identity = %s, source_fingerprint = %s, source_row_number = %s, source_order_upload_id = %s
                    WHERE id = %s
                    """,
                    changed_client_data_params
                )
            if changed_verification_params:
                execute_batch_pg(
                    conn,
                    "UPDATE verification_details SET validation_status = 'Pending' WHERE id = %s",
                    changed_verification_params
                )
            if changed_audit_params:
                execute_batch_pg(conn, DATA_AUDIT_LOG_INSERT_SQL, changed_audit_params)

            if changed_classification_sources:
                upsert_classification_rows_postgres(
                    conn,
                    changed_classification_sources,
                    treasury_mapping,
                    configuration_version=1,
                )
            # 3. New: insert fresh rows
            new_client_data_params = []
            new_row_data_list = []
            new_extract_dates = []
            new_row_identities = change_detection.get("new_source_identities", [])
            if len(new_row_identities) != len(change_detection["new"]):
                new_row_identities = _canonical_source_identities(
                    template_cols,
                    change_detection["new"],
                    request.template_id,
                )
            for source_index, (new_row_data, source_identity) in enumerate(zip(
                change_detection["new"], new_row_identities
            )):
                amount, date_str = extract_columns_from_row(new_row_data, template_cols, effective_delimiter, effective_order)
                doc_group_id = get_document_group_id(template_cols, new_row_data, request.template_id)
                source_fingerprint = _row_fingerprint_token(new_row_data)
                # data_quality_flag (Phase 4 of the blank-field-rejection requirement) --
                # 'incomplete' for a force-included row still carrying a blank critical field.
                data_quality_flag = compute_data_quality_flag(new_row_data, critical_cols)
                new_client_data_params.append(
                    (request.client_id, request.template_id, request.file_id,
                     Json(new_row_data, dumps=lambda d: json.dumps(d, default=str)), amount, date_str, "Analyst", 1, doc_group_id, data_quality_flag, rate_source, source_identity, source_fingerprint,
                     (change_detection.get('new_source_row_numbers') or [None] * len(new_row_identities))[source_index], request.file_id)
                )
                new_row_data_list.append(new_row_data)
                new_extract_dates.append(date_str)
                inserted_count += 1
                IMPORT_PROGRESS[request.file_id]["current"] += 1

            if new_client_data_params:
                new_ids = execute_batch_insert_with_ids_pg(
                    conn,
                    """
                    INSERT INTO client_data (client_id, template_id, upload_id, row_data, extract_amount, extract_date, created_by, version, change_detection_status, document_group_id, data_quality_flag, rate_source, source_identity, source_fingerprint, source_row_number, source_order_upload_id)
                    VALUES %s RETURNING id
                    """,
                    new_client_data_params,
                    template="(%s, %s, %s, %s, %s, %s, %s, %s, 'active', %s, %s, %s, %s, %s, %s, %s)"
                )
                new_classification_sources = [
                    {
                        "id": row_id,
                        "client_id": request.client_id,
                        "template_id": request.template_id,
                        "upload_id": request.file_id,
                        "row_data": new_row_data,
                        "extract_date": extract_date,
                    }
                    for row_id, new_row_data, extract_date in zip(
                        new_ids, new_row_data_list, new_extract_dates
                    )
                ]
                upsert_classification_rows_postgres(
                    conn,
                    new_classification_sources,
                    treasury_mapping,
                    configuration_version=1,
                )
                new_audit_params = [
                    _audit_row_params(row_id, "INSERT", None, new_row_data)
                    for row_id, new_row_data in zip(new_ids, new_row_data_list)
                ]
                execute_batch_pg(conn, DATA_AUDIT_LOG_INSERT_SQL, new_audit_params)

            # Replacement scope is explicit; protected references block the
            # whole import rather than leaving retained soft-deleted copies.
            if change_detection["missing"]:
                try:
                    replacement_cleanup = purge_replaced_transaction_rows(
                        request.client_id, request.template_id,
                        [item["client_data_id"] for item in change_detection["missing"]], "Analyst",
                    )
                    deleted_count = replacement_cleanup["deleted_count"]
                except DataPurgeProtectedError as error:
                    raise HTTPException(409, str(error)) from error
                IMPORT_PROGRESS[request.file_id]["current"] += deleted_count

        else:
            # --- First-time upload (no existing data with valid composite key): insert all rows as new ---
            first_upload_client_data_params = []
            first_upload_row_data_list = []
            first_upload_extract_dates = []
            first_upload_occurrences = {}
            for source_index, row in enumerate(rows, 2):
                # Build JSON row_data
                row_data = {}
                for t_col_name, excel_col in col_mapping.items():
                    if excel_col in row:
                        row_data[t_col_name] = json_safe_cell_value(row[excel_col], preserve_native_dates=True)

                # Rate derivation (Phase 7 of the Rate-derivation requirement) -- see
                # apply_rate_derivation()'s docstring for why this must happen right
                # here, at first assembly.
                apply_rate_derivation(row_data, template_cols, rate_source, rate_precision)
                # Restores the template's real column order -- apply_rate_derivation() may have
                # just appended 'Rate' at the end of the dict (see reorder_row_data_by_template()'s
                # docstring).
                row_data = reorder_row_data_by_template(row_data, template_cols)

                # Compute document_group_id from the first column value
                doc_group_id = get_document_group_id(template_cols, row_data, request.template_id)
                occurrence_key = (doc_group_id, _row_fingerprint_token(row_data))
                first_upload_occurrences[occurrence_key] = (
                    first_upload_occurrences.get(occurrence_key, 0) + 1
                )
                source_identity = _canonical_source_identity(
                    template_cols,
                    row_data,
                    request.template_id,
                    occurrence_index=first_upload_occurrences[occurrence_key],
                )
                source_fingerprint = _row_fingerprint_token(row_data)

                # Extract amount and date for indexing
                amount, date_str = extract_columns_from_row(row_data, template_cols, effective_delimiter, effective_order)

                # data_quality_flag (Phase 4 of the blank-field-rejection requirement) --
                # 'incomplete' for a force-included row still carrying a blank critical field.
                data_quality_flag = compute_data_quality_flag(row_data, critical_cols)

                first_upload_client_data_params.append(
                    (request.client_id, request.template_id, request.file_id,
                     Json(row_data, dumps=lambda d: json.dumps(d, default=str)), amount, date_str, "Analyst", 1, doc_group_id, data_quality_flag, rate_source, source_identity, source_fingerprint, _ledger_row_number(row, source_index), request.file_id)
                )
                first_upload_row_data_list.append(row_data)
                first_upload_extract_dates.append(date_str)
                inserted_count += 1
                IMPORT_PROGRESS[request.file_id]["current"] += 1

            if first_upload_client_data_params:
                first_upload_ids = execute_batch_insert_with_ids_pg(
                    conn,
                    """
                    INSERT INTO client_data (client_id, template_id, upload_id, row_data, extract_amount, extract_date, created_by, version, change_detection_status, document_group_id, data_quality_flag, rate_source, source_identity, source_fingerprint, source_row_number, source_order_upload_id)
                    VALUES %s RETURNING id
                    """,
                    first_upload_client_data_params,
                    template="(%s, %s, %s, %s, %s, %s, %s, %s, 'active', %s, %s, %s, %s, %s, %s, %s)"
                )
                first_classification_sources = [
                    {
                        "id": row_id,
                        "client_id": request.client_id,
                        "template_id": request.template_id,
                        "upload_id": request.file_id,
                        "row_data": row_data,
                        "extract_date": extract_date,
                    }
                    for row_id, row_data, extract_date in zip(
                        first_upload_ids, first_upload_row_data_list, first_upload_extract_dates
                    )
                ]
                upsert_classification_rows_postgres(
                    conn,
                    first_classification_sources,
                    treasury_mapping,
                    configuration_version=1,
                )
                first_upload_audit_params = [
                    _audit_row_params(row_id, "INSERT", None, row_data)
                    for row_id, row_data in zip(first_upload_ids, first_upload_row_data_list)
                ]
                execute_batch_pg(conn, DATA_AUDIT_LOG_INSERT_SQL, first_upload_audit_params)

        # Derived canonical decisions share this import transaction. A failure
        # rolls back the configured Cashflows upload rather than leaving a
        # successful source write without its integrity decision.
        cashflow_voucher_resolution = resolve_upload_occurrences_postgres(
            conn,
            client_id=request.client_id,
            template_id=request.template_id,
            upload_id=request.file_id,
            row_data=cashflow_occurrence_rows,
        )
    if replacement_cleanup["staged_paths"]:
        failures = _remove_staged_upload_files(replacement_cleanup["staged_paths"])
        if failures:
            logger.warning("Replacement staged-file cleanup requires retry (%s files)", len(failures))
    legacy_snapshot_required = (
        cashflow_voucher_resolution is None
        or not cashflow_voucher_resolution.get("eligible")
        or cashflow_voucher_resolution.get("cache_invalidation_required")
        or change_detection.get('source_order_refresh_required', False)
    )
    preparation_required = bool(
        cashflow_voucher_resolution
        and cashflow_voucher_resolution.get("preparation_required")
    )
    if legacy_snapshot_required:
        invalidate_cashflow_summary_read_models(
            request.client_id,
            reason="source_imported",
            changed_by="Analyst",
            write_fn=execute_write_pg,
        )
    preparation_job = None
    preparation_warning = None
    IMPORT_PROGRESS[request.file_id]["status"] = "preparing"
    if legacy_snapshot_required or preparation_required:
        if preparation_required:
            preparation_job, preparation_warning = _enqueue_upload_preparation(
                request, cashflow_voucher_resolution
            )
        if background_tasks is None:
            # Direct callers and isolated tests retain the old synchronous contract.
            _prepare_cashflow_snapshot_background(
                request.client_id,
                request.file_id,
                request.template_id,
                cashflow_voucher_resolution,
                preparation_job["job_id"] if preparation_job else None,
            )
        else:
            background_tasks.add_task(
                _prepare_cashflow_snapshot_background,
                request.client_id,
                request.file_id,
                request.template_id,
                cashflow_voucher_resolution,
                preparation_job["job_id"] if preparation_job else None,
            )
    else:
        IMPORT_PROGRESS[request.file_id]["status"] = "complete"
    log_audit(
        "Analyst",
        "Import",
        "Upload",
        f"Imported {inserted_count} added, {updated_count} changed, {deleted_count} removed for client {request.client_id}, template {template[0]['template_name']}, file ID {request.file_id}"
    )

    if background_tasks is None:
        IMPORT_PROGRESS[request.file_id]["status"] = "complete"

    _persist_upload_summary(
        request,
        change_detection,
        total_rows_attempted,
        status="Imported",
    )
    upload_summary = _build_upload_summary(
        file_info[0],
        template[0],
        request,
        change_detection,
        total_rows_attempted,
        status="Imported",
    )

    return {
        "status": "success",
        "source_diagnostics": source_diagnostics,
        "message": f"Successfully imported: {inserted_count} added, {updated_count} changed, {deleted_count} removed",
        "total_rows": total_rows_attempted,
        "imported_count": inserted_count,
        "updated_count": updated_count,
        "deleted_count": deleted_count,
        "validation_errors": [],
        "rejected_rows": rejected_rows,  # Phase 2 of the blank-field-rejection requirement
        "rejected_count": len(rejected_rows),
        # Phase 4: of the rows above, how many were force-included via the Exceptions
        # override (imported with the critical field genuinely blank, flagged
        # 'incomplete') -- the frontend derives "truly rejected/excluded" as
        # rejected_count - force_included_count.
        "force_included_count": force_included_count,
        "change_detection": public_changes,
        "upload_summary": upload_summary,
        "cashflow_voucher_resolution": cashflow_voucher_resolution,
        "cashflow_preparation_job": preparation_job,
        "cashflow_preparation_warning": preparation_warning,
    }

@router.get("/api/upload/import-status/{file_id}")
def get_import_status(file_id: int):
    """Polled by the frontend while POST /api/upload/import is in flight for this file_id."""
    return IMPORT_PROGRESS.get(file_id, {"current": 0, "total": 0, "status": "idle"})

# ---------- DOWNLOAD TEMPLATE ----------
@router.get("/api/upload/download-template/{template_id}")
def download_template(template_id: int):
    template = execute_query_pg("SELECT id, template_name FROM templates WHERE id = %s AND is_active = TRUE", (template_id,))
    if not template:
        raise HTTPException(404, "Template not found")
    columns = execute_query_pg(
        """
        SELECT m.column_name, m.data_type, p.reconciliation_role, p.compare_for_changes, p.comparison_review_required
        FROM template_column_mappings p
        JOIN master_columns m ON p.column_id = m.id
        WHERE p.template_id = %s AND m.is_active = TRUE
        ORDER BY p.sort_order ASC
        """,
        (template_id,)
    )
    if not columns:
        raise HTTPException(400, "Template has no columns")

    output = BytesIO()
    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        header_row = [col['column_name'] for col in columns]
        sample_row = []
        for col in columns:
            if col['data_type'] == "Numeric":
                sample_row.append(1000)
            elif col['data_type'] == "Date":
                sample_row.append("01/01/2025")
            else:
                sample_row.append("Sample")
        df = pd.DataFrame([sample_row], columns=header_row)
        df.to_excel(writer, index=False, sheet_name='Template')

        workbook = writer.book
        instructions = workbook.create_sheet("Instructions")
        instructions.append(["📋 Template Instructions"])
        instructions.append([""])
        instructions.append(["1. This template contains the columns required for import."])
        instructions.append(["2. The column headers match the template defined in the system."])
        instructions.append(["3. The sample row shows the expected data format for each column."])
        instructions.append(["4. Required columns must be filled."])
        instructions.append([""])
        instructions.append(["📌 Data Type Guidelines:"])
        instructions.append(["- Text: Any alphanumeric value"])
        instructions.append(["- Numeric: Numbers only (e.g., 1000, 25.50)"])
        instructions.append(["- Date: Format DD/MM/YYYY (e.g., 25/12/2025)"])
        instructions.append([""])
        instructions.append(["⚠️ Important:"])
        instructions.append(["- Do not change the column headers."])
        instructions.append(["- Delete the sample row before adding your data."])
        instructions.append(["- Save the file as .xlsx before uploading."])

        for sheet in workbook.sheetnames:
            ws = workbook[sheet]
            for col in ws.columns:
                max_len = 0
                col_letter = col[0].column_letter
                for cell in col:
                    try:
                        if len(str(cell.value)) > max_len:
                            max_len = len(str(cell.value))
                    except:
                        pass
                ws.column_dimensions[col_letter].width = min(max_len + 2, 50)

    output.seek(0)
    return StreamingResponse(
        output,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename=template_{template[0]['template_name']}.xlsx"}
    )

# ---------- GET FILE DETAILS ----------
@router.get("/api/upload/file/{file_id}")
def get_file_details(file_id: int):
    file_info = execute_query_pg("SELECT * FROM uploaded_files WHERE id = %s", (file_id,))
    if not file_info:
        raise HTTPException(404, "File not found")
    return file_info[0]
