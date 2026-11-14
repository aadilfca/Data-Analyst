"""
Trial Balance module backend (original 5-phase build complete; now under a
new Column mapping & standard template requirement - Section 1, Phase 1 of
this file's own changes is done: explicit mapping payload + suggest-mapping
endpoint).

New, dedicated feature - deliberately not an extension of import_export.py/
templates.py (those handle generic template-mapped imports; TB needs
hierarchy-aware parent/leaf classification derived from the account code
structure, not client-specific text markers). As of Section 1/Phase 1,
columns are identified by an explicit column-mapping payload the caller
supplies (which real Excel column is which field) rather than hardcoded
header-name matching - see validate_column_mapping() below. The standard
mapping is 8 required fields (Account Code, Account Name, Opening Dr/Cr,
Period Dr/Cr, Closing Dr/Cr), plus one optional field (Type, a cross-check-only
display value). The normalized synonym lists (clean_header(), MAPPING_ROLES)
are kept and reused only for suggest_column_mapping()'s best-guess pre-fill,
not for auto-driving the upload itself.

Endpoints: mapping suggestion, upload+classify with confirmed code corrections,
account and summary reads, selected-upload account-code correction, and manual
classification override.
"""

import json
import math
import re
from datetime import datetime
from decimal import Decimal, InvalidOperation
from io import BytesIO
from typing import Optional

import pandas as pd
from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from database import execute_query_pg, execute_write_pg, execute_batch_pg, get_raw_conn_pg, release_raw_conn_pg, log_audit, pg_transaction
from psycopg2.extras import Json
from routers.auth import require_admin
from routers.audit_rules import invalidate_category_for_account_cache

router = APIRouter()

# In-memory row-parse progress for an in-flight TB upload, keyed by a client-generated
# progress_key (TB upload is a single one-shot endpoint, unlike Data Bank's separate
# "upload file -> get file_id -> import" flow, so there's no server-issued id to reuse) -
# same pattern as import_export.py's IMPORT_PROGRESS/GET /api/upload/import-status/{file_id}.
# Synchronous single-request upload, no queue/background job - state only lives for the
# duration of one upload call.
TB_UPLOAD_PROGRESS: dict = {}


class AccountOverrideRequest(BaseModel):
    """Body for the Phase 5 row-level classification override endpoint."""
    is_leaf: bool


class AccountCodeEditRequest(BaseModel):
    """Body for correcting an account code within one selected upload."""
    account_code: str


class AccountCodeCorrection(BaseModel):
    account_id: int
    account_code: str
    expected_account_code: str


class AccountCodeBatchRequest(BaseModel):
    corrections: list[AccountCodeCorrection]


class TrialBalanceColumnMappingPayload(BaseModel):
    """One explicitly saved Trial Balance column mapping for a client."""
    client_id: int
    mapping: dict[str, str]


class CategoryAssignmentRequest(BaseModel):
    """Body for the Assign Categories cascade-assignment endpoint (Section 2,
    Phase 4). force_apply defaults to False (smart-fill) - True (force-apply)
    is only ever sent via the explicit UI icon in Phase 5, never the default
    path. category_id is nullable (confirmed fix) - null means "Not assigned":
    selecting it via the normal path clears just the target account, and
    selecting it with force_apply=True clears the target and cascades that
    clearing to every descendant, same rule as assigning a real category."""
    account_id: int
    category_id: Optional[int] = None
    force_apply: bool = False

# ---------- COLUMN IDENTIFICATION ----------
# Trial Balance columns are now identified via an explicit column-mapping
# payload supplied by the caller (Section 1, Phase 1) rather than hardcoded
# header-name matching alone. The synonym sets/clean_header() below are kept
# and reused for suggest_column_mapping() - a best-guess suggestion endpoint
# the frontend mapping window (Phase 2) uses to pre-fill likely matches; they
# no longer drive the upload endpoint's own column identification directly.
CODE_SYNONYMS = {"code", "accountcode", "ledgercode", "accode", "glcode", "accountno", "acno"}
NAME_SYNONYMS = {"name", "accountname", "particulars", "description", "accounttitle", "title"}
TYPE_SYNONYMS = {"type", "mastersub", "accounttype", "classification"}
# Standard 8-field trial balance mapping fix: the original single Debit/Credit pair
# is replaced by 3 Dr/Cr pairs (Opening, Period, Closing) - Period Dr/Cr is the
# direct successor of the old Debit/Credit synonyms (bare "debit"/"dr"/"credit"/"cr"
# only ever mean the current-period movement on a real trial balance), Opening/
# Closing are new pairs alongside it.
OPENING_DR_SYNONYMS = {"openingdr", "openingdebit", "openingbalancedr", "openingbaldr", "obdr", "openingdramount"}
OPENING_CR_SYNONYMS = {"openingcr", "openingcredit", "openingbalancecr", "openingbalcr", "obcr", "openingcramount"}
PERIOD_DR_SYNONYMS = {"perioddr", "perioddebit", "debit", "dr", "debitamount", "duringperioddebit", "movementdr", "transactiondebit"}
PERIOD_CR_SYNONYMS = {"periodcr", "periodcredit", "credit", "cr", "creditamount", "duringperiodcredit", "movementcr", "transactioncredit"}
CLOSING_DR_SYNONYMS = {"closingdr", "closingdebit", "closingbalancedr", "closingbaldr", "cbdr", "closingdramount"}
CLOSING_CR_SYNONYMS = {"closingcr", "closingcredit", "closingbalancecr", "closingbalcr", "cbcr", "closingcramount"}

# Priority order shared by both suggest_column_mapping() and the required-field
# list in validate_column_mapping() - code/name first (required), then the 6
# amount roles (all required, standard 8-field mapping fix), then the one
# optional role (type_column), so no header is ever claimed by two roles at once.
MAPPING_ROLES = [
    ("account_code_column", CODE_SYNONYMS),
    ("account_name_column", NAME_SYNONYMS),
    ("type_column", TYPE_SYNONYMS),
    ("opening_dr_column", OPENING_DR_SYNONYMS),
    ("opening_cr_column", OPENING_CR_SYNONYMS),
    ("period_dr_column", PERIOD_DR_SYNONYMS),
    ("period_cr_column", PERIOD_CR_SYNONYMS),
    ("closing_dr_column", CLOSING_DR_SYNONYMS),
    ("closing_cr_column", CLOSING_CR_SYNONYMS),
]
REQUIRED_MAPPING_ROLES = [
    "account_code_column", "account_name_column",
    "opening_dr_column", "opening_cr_column",
    "period_dr_column", "period_cr_column",
    "closing_dr_column", "closing_cr_column",
]

# Human-readable labels for validation error messages.
MAPPING_ROLE_LABELS = {
    "account_code_column": "Account Code",
    "account_name_column": "Account Name",
    "type_column": "Type",
    "opening_dr_column": "Opening Dr",
    "opening_cr_column": "Opening Cr",
    "period_dr_column": "Period Dr",
    "period_cr_column": "Period Cr",
    "closing_dr_column": "Closing Dr",
    "closing_cr_column": "Closing Cr",
}

# Section 1, Phase 3: header row (in mapping order) for the downloadable standard
# template - the escape hatch for a source file too irregular to map cleanly. Only
# the 8 required fields (Type is optional and not part of the "standard" shape).
# No versioning: if the 8-field standard changes again, this list is simply updated
# and the template is regenerated - there's no "v2" concept, per the locked scope.
STANDARD_TEMPLATE_HEADERS = [
    "Account Code", "Account Name",
    "Opening Dr", "Opening Cr",
    "Period Dr", "Period Cr",
    "Closing Dr", "Closing Cr",
]


def clean_header(text) -> str:
    """Normalize a raw column header for synonym matching: lowercase, strip
    every non-alphanumeric character (spaces, '/', '.', etc.), so "A/C Code",
    "Account Code", and "account_code" all normalize to the same string."""
    return re.sub(r'[^a-z0-9]', '', str(text).lower())


def read_trial_balance_excel(contents: bytes, filename: str) -> pd.DataFrame:
    """
    Shared file-reading helper used by both the upload endpoint and the
    suggest-mapping endpoint, so the two can never disagree on how a sheet is
    read. dtype=str converts parsed values to text but cannot recover an
    intended account code when Excel has already stored that cell as a date;
    the upload review separately inspects workbook cell types and lets the
    user confirm a correction. keep_default_na=False maps blanks to '' rather
    than NaN.
    """
    engine = "xlrd" if filename.lower().endswith('.xls') else "openpyxl"
    try:
        return pd.read_excel(BytesIO(contents), dtype=str, keep_default_na=False, engine=engine)
    except Exception as e:
        raise ValueError(f"Could not read the Excel file: {e}")


def suggest_column_mapping(headers: list) -> dict:
    """
    Best-guess suggestion for every mapping role, for the frontend mapping
    window (Phase 2) to pre-fill. For each role (in MAPPING_ROLES priority
    order, so no header is claimed by two roles): first try an exact
    normalized-synonym match (same precision as the old identify_columns()),
    then fall back to a bidirectional substring match between the normalized
    header and any synonym - mirroring templates.py's get_close_matches()
    ("typed in x or x in typed"), not a fuzzy-matching library. Returns
    {role: header_name_or_None} for every role - purely a suggestion, never
    used to silently drive the upload itself.
    """
    used_headers = set()
    suggestions = {}
    for role, synonyms in MAPPING_ROLES:
        match = None
        # Pass 1: exact normalized match.
        for header in headers:
            if header in used_headers:
                continue
            if clean_header(header) in synonyms:
                match = header
                break
        # Pass 2: bidirectional substring fallback.
        if match is None:
            for header in headers:
                if header in used_headers:
                    continue
                normalized = clean_header(header)
                if any(normalized in syn or syn in normalized for syn in synonyms):
                    match = header
                    break
        suggestions[role] = match
        if match is not None:
            used_headers.add(match)
    return suggestions


def validate_column_mapping(mapping: dict, headers: list) -> None:
    """
    Validate a caller-supplied column mapping against the file's actual
    headers. Raises ValueError naming every problem at once (not just the
    first one hit) if: a required role (Account Code/Name/Debit/Credit) is
    missing/blank, or any provided mapped column name (required or optional)
    doesn't exactly match a real header in the file. The caller's outer
    except Exception block turns this into a clean 400 + an 'error'-status
    upload row, rather than a raw crash deeper in the classification pipeline.
    """
    problems = []
    header_set = set(headers)

    for role in REQUIRED_MAPPING_ROLES:
        value = mapping.get(role)
        if not value or not str(value).strip():
            problems.append(f"{MAPPING_ROLE_LABELS[role]} column mapping is required.")

    for role, _synonyms in MAPPING_ROLES:
        value = mapping.get(role)
        if value and value not in header_set:
            problems.append(
                f"{MAPPING_ROLE_LABELS[role]} is mapped to '{value}', which is not "
                f"a column in the uploaded file."
            )

    mapped_required = [mapping.get(role) for role in REQUIRED_MAPPING_ROLES if mapping.get(role)]
    if len(mapped_required) != len(set(mapped_required)):
        problems.append("Each required Trial Balance field must use a different Excel column.")

    if problems:
        raise ValueError(" ".join(problems))


def _mapped_raw_row_data(row: dict, mapping: dict) -> dict:
    """Keep only explicitly mapped source columns in the stored raw-row copy."""
    mapped_columns = dict.fromkeys(
        column for column in mapping.values()
        if isinstance(column, str) and column.strip()
    )
    return {column: row.get(column) for column in mapped_columns}


def parse_amount(raw_value):
    """
    Best-effort parse of a Debit/Credit cell into a float. Never raises -
    amounts are secondary to classification this phase, so an unparsable or
    blank value simply becomes None rather than blocking the whole upload.
    Handles thousands separators (",") and the accounting parenthesis-negative
    convention (e.g. "(1,234.56)" -> -1234.56).
    """
    if raw_value is None:
        return None
    text = str(raw_value).strip()
    if text == '':
        return None
    negative = text.startswith('(') and text.endswith(')')
    if negative:
        text = text[1:-1]
    text = text.replace(',', '').strip()
    try:
        value = float(text)
    except ValueError:
        return None
    return -value if negative else value


def _parse_validation_amount(raw_value):
    """Return a precise Decimal, zero for an empty cell, or None for bad input."""
    text = str(raw_value or '').strip()
    if not text:
        return Decimal('0')
    negative = text.startswith('(') and text.endswith(')')
    if negative:
        text = text[1:-1]
    text = text.replace(',', '').strip()
    try:
        value = Decimal(text)
    except InvalidOperation:
        return None
    if not value.is_finite():
        return None
    return -value if negative else value


def _load_row_overrides(raw_value, row_count, allowed_values, label):
    if isinstance(raw_value, dict):
        parsed = raw_value
    else:
        try:
            parsed = json.loads(raw_value or '{}')
        except json.JSONDecodeError as exc:
            raise ValueError(f'{label} were invalid. Reopen the file review and try again.') from exc
    if not isinstance(parsed, dict):
        raise ValueError(f'{label} must be supplied by Excel row number.')
    result = {}
    for row_key, value in parsed.items():
        try:
            row_number = int(row_key)
        except (TypeError, ValueError) as exc:
            raise ValueError(f'{label} contain an invalid Excel row number.') from exc
        if row_number < 2 or row_number > row_count + 1:
            raise ValueError(f'{label} refer to row {row_number}, outside the uploaded file.')
        value = str(value or '').strip()
        if allowed_values is not None and value not in allowed_values:
            raise ValueError(f'{label} for Excel row {row_number} must be one of: {", ".join(sorted(allowed_values))}.')
        if allowed_values is None and (not value or len(value) > 120):
            raise ValueError(f'Account code on Excel row {row_number} must contain 1 to 120 characters.')
        result[row_number] = value
    return result


def _load_excluded_excel_rows(raw_value, row_count):
    """Validate source subtotal exclusions supplied by original Excel row number."""
    try:
        parsed = json.loads(raw_value or '[]') if isinstance(raw_value, str) or raw_value is None else raw_value
    except json.JSONDecodeError as exc:
        raise ValueError('Excluded subtotal rows were invalid. Reopen the file review and try again.') from exc
    if not isinstance(parsed, list):
        raise ValueError('Excluded subtotal rows must be supplied as an Excel row-number list.')

    excluded = set()
    for value in parsed:
        if isinstance(value, bool):
            raise ValueError('Excluded subtotal rows contain an invalid Excel row number.')
        try:
            row_number = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError('Excluded subtotal rows contain an invalid Excel row number.') from exc
        if str(row_number) != str(value).strip():
            raise ValueError('Excluded subtotal rows contain an invalid Excel row number.')
        if row_number < 2 or row_number > row_count + 1:
            raise ValueError(f'Excluded subtotal row {row_number} is outside the uploaded file.')
        if row_number in excluded:
            raise ValueError(f'Excluded subtotal row {row_number} was supplied more than once.')
        excluded.add(row_number)
    return excluded


def _build_draft_reconciliation(original, adjusted, tolerance=Decimal('0.01')):
    """Summarize existing entries only; this never creates balancing adjustments."""
    roles = [period + '_' + side + '_column'
             for period in ('opening', 'period', 'closing') for side in ('dr', 'cr')]
    return {
        'original_totals': {role: str(original[role]) for role in roles},
        'adjustment_totals': {role: str(adjusted[role] - original[role]) for role in roles},
        'adjusted_totals': {role: str(adjusted[role]) for role in roles},
        'pair_differences': {
            period: {'difference': str(adjusted[period + '_dr_column'] - adjusted[period + '_cr_column']),
                     'within_tolerance': abs(adjusted[period + '_dr_column'] - adjusted[period + '_cr_column']) <= tolerance}
            for period in ('opening', 'period', 'closing')
        },
    }


def _erp_totals_unbalanced(totals, tolerance=Decimal('0.01')):
    """ERP pair balance is distinct from matching original import footings."""
    if not totals:
        return None
    return any(abs(totals[period + '_dr_column'] - totals[period + '_cr_column']) > tolerance
               for period in ('opening', 'period', 'closing'))


def validate_trial_balance_contents(
    contents, filename, mapping, code_overrides=None, classification_overrides=None, excluded_excel_rows=None,
    total_excel_row=None, no_total_row=False
):
    """Parse and reconcile a Trial Balance without writing any database records."""
    df = read_trial_balance_excel(contents, filename)
    headers = df.columns.tolist()
    validate_column_mapping(mapping, headers)
    rows = df.to_dict('records')
    code_overrides = _load_row_overrides(code_overrides, len(rows), None, 'Account code corrections')
    classification_overrides = _load_row_overrides(
        classification_overrides, len(rows), {'leaf', 'parent'}, 'Classification corrections'
    )
    excluded_excel_rows = _load_excluded_excel_rows(excluded_excel_rows, len(rows))
    errors = []
    candidates = []
    seen_codes = {}
    invalid_amounts = False
    amount_roles = (
        ('opening_dr_column', 'Opening Dr'), ('opening_cr_column', 'Opening Cr'),
        ('period_dr_column', 'Period Dr'), ('period_cr_column', 'Period Cr'),
        ('closing_dr_column', 'Closing Dr'), ('closing_cr_column', 'Closing Cr'),
    )

    def add_error(excel_row, field, message):
        issue = {'excel_row': excel_row, 'field': field, 'message': message, 'severity': 'error'}
        errors.append(issue)

    def add_warning(excel_row, field, message):
        issue = {'excel_row': excel_row, 'field': field, 'message': message, 'severity': 'warning'}
        warnings.append(issue)

    # The reviewer identifies the control row explicitly; its label and position
    # are never used to guess whether it is an account or an ERP total.
    control_rows = []
    if no_total_row:
        if total_excel_row is not None and str(total_excel_row).strip():
            raise ValueError('Choose either an ERP total row number or No total row, not both.')
    else:
        value = str(total_excel_row or '').strip()
        if not re.fullmatch(r'[0-9]+', value):
            raise ValueError('Enter the Excel row number of Total, or explicitly select No total row.')
        row_number = int(value)
        if row_number < 2 or row_number > len(rows) + 1:
            raise ValueError(f'Total row must be an Excel row from 2 to {len(rows) + 1}.')
        if row_number in code_overrides or row_number in classification_overrides:
            raise ValueError('The selected Total row has account corrections. Clear them or choose another Total row.')
        row = rows[row_number - 2]
        if not any(str(row.get(mapping[role], '') or '').strip() for role, _ in amount_roles):
            raise ValueError('The selected Total row contains no mapped amounts. Choose its actual Excel row number.')
        amounts = {role: _parse_validation_amount(row.get(mapping[role])) for role, _ in amount_roles}
        if any(value is None for value in amounts.values()):
            raise ValueError('The selected Total row contains an invalid mapped amount. Correct the source file or row selection.')
        control_rows.append({'excel_row': row_number, 'amounts': amounts})
    control_row_numbers = {row['excel_row'] for row in control_rows}
    excluded_excel_rows -= control_row_numbers

    date_code_pattern = re.compile(r'^(\d{4})-(\d{1,2})-(\d{1,2})(?: 00:00:00)?$')
    for index, row in enumerate(rows):
        excel_row = index + 2
        if excel_row in control_row_numbers:
            continue
        source_code = str(row.get(mapping['account_code_column'], '') or '').strip()
        code = str(code_overrides.get(excel_row, source_code) or '').strip()
        row_has_data = any(str(row.get(column, '') or '').strip() for column in mapping.values() if column)
        if not row_has_data:
            if excel_row in excluded_excel_rows:
                raise ValueError(f'Excel row {excel_row} is empty and cannot be marked as a subtotal.')
            continue
        if excel_row in excluded_excel_rows:
            if source_code:
                raise ValueError(f'Excel row {excel_row} has an Account Code and cannot be excluded as a subtotal.')
            if code_overrides.get(excel_row):
                raise ValueError(f'Excel row {excel_row} has an account-code correction and cannot be excluded as a subtotal.')
            if excel_row in classification_overrides:
                raise ValueError(f'Excel row {excel_row} has a classification correction and cannot be excluded as a subtotal.')
            continue
        if not code:
            add_error(excel_row, 'Account Code', 'Account code is blank.')
            continue
        if len(code) > 120:
            add_error(excel_row, 'Account Code', 'Account code cannot exceed 120 characters.')
        date_match = date_code_pattern.match(source_code)
        if date_match and excel_row not in code_overrides:
            try:
                date_value = datetime(int(date_match.group(1)), int(date_match.group(2)), int(date_match.group(3)))
                add_error(excel_row, 'Account Code', 'This code appears to have been converted to an Excel date. Check the source workbook and correct it if needed.')
            except ValueError:
                pass
        if code in seen_codes:
            add_error(excel_row, 'Account Code', f'Account code {code} duplicates Excel row {seen_codes[code]}.')
        else:
            seen_codes[code] = excel_row

        amount_values = {}
        for role, label in amount_roles:
            raw_value = row.get(mapping[role])
            parsed_value = _parse_validation_amount(raw_value)
            if parsed_value is None:
                invalid_amounts = True
                add_error(excel_row, label, f'{label} must be a number or blank; found {str(raw_value).strip()!r}.')
                amount_values[role] = Decimal('0')
            else:
                amount_values[role] = parsed_value
        candidates.append({
            'excel_row': excel_row,
            'account_code': code,
            'source_account_code': source_code,
            'classification': None,
            'amounts': amount_values,
        })

    if not candidates:
        add_error(None, 'Rows', 'No Trial Balance account rows were found in the selected sheet.')

    code_set = {row['account_code'] for row in candidates}
    auto_classification = classify_accounts(code_set)
    for account in candidates:
        row_number = account['excel_row']
        auto_value = auto_classification.get(account['account_code'], 'leaf')
        account['classification'] = classification_overrides.get(row_number, auto_value)
        account['auto_classification'] = auto_value

    # Only code-format findings on rows classified as Parent are advisory. The
    # current hierarchy rule remains authoritative; broader parent/leaf detection
    # is a separate phase. Duplicate identities remain blocking for every row.
    warnings = []
    blocking_errors = []
    parent_rows = {
        account['excel_row'] for account in candidates
        if account['classification'] == 'parent'
    }
    for issue in errors:
        is_duplicate = issue['message'].startswith('Account code ') and ' duplicates Excel row ' in issue['message']
        if issue['field'] == 'Account Code' and issue['excel_row'] in parent_rows and not is_duplicate:
            issue['severity'] = 'warning'
            warnings.append(issue)
        else:
            blocking_errors.append(issue)
    errors = blocking_errors

    for account in candidates:
        if account['classification'] == 'parent' and account['auto_classification'] != 'parent':
            warnings.append({'excel_row': account['excel_row'], 'field': 'Account Code', 'severity': 'warning',
                             'message': 'Parent has no linked child accounts. Correct the code or confirm it is intentional before import.'})

    valid_amount_rows = [row for row in candidates if row['classification'] == 'leaf']
    if not valid_amount_rows:
        add_error(None, 'Rows', 'At least one Leaf account is required for validation.')
    tolerance = Decimal('0.01')
    checks = [{'key': 'account_codes', 'label': 'Account codes and leaf selection',
               'status': 'fail' if errors else 'pass'}]
    source_totals = {role: sum((r['amounts'][role] for r in valid_amount_rows), Decimal('0'))
                     for role, _ in amount_roles}
    source_status = 'not_applicable' if not control_rows else 'blocked'
    source_comparison = []
    if control_rows and not errors:
        for role, label in amount_roles:
            total = control_rows[0]['amounts'][role]
            difference = source_totals[role] - total
            source_comparison.append({'field': label, 'source_total': str(total),
                                      'leaf_total': str(source_totals[role]), 'difference': str(difference),
                                      'within_tolerance': abs(difference) <= tolerance})
        source_status = 'pass' if all(abs(Decimal(r['difference'])) <= tolerance for r in source_comparison) else 'fail'
        if source_status == 'fail':
            add_error(None, 'ERP total', 'Imported leaf totals differ from the ERP total. Review mapping and leaf selection, or correct and re-import the source file.')
    checks.append({'key': 'source_total', 'label': 'Original leaf totals match ERP total',
                   'status': source_status, 'columns': source_comparison})
    rollforward_differences = []
    if not errors:
        for account in valid_amount_rows:
            adjustment = calculate_rollforward_adjustment(account['amounts'], tolerance)
            if adjustment:
                rollforward_differences.append({
                    'excel_row': account['excel_row'], 'account_code': account['account_code'],
                    'account_name': str(rows[account['excel_row'] - 2].get(mapping['account_name_column'], '') or '').strip() or None,
                    **{key: float(value) for key, value in adjustment.items()},
                    **{f'{key}_exact': str(value) for key, value in adjustment.items()},
                })
                add_warning(account['excel_row'], 'Roll-forward', 'Opening balance plus period movement does not equal closing balance within 0.01.')
    checks.append({'key': 'roll_forward', 'label': 'Opening plus period movement equals closing',
                   'status': 'blocked' if errors else ('adjusted' if rollforward_differences else 'pass'),
                   'affected_rows': len(rollforward_differences), 'tolerance': 0.01})
    adjusted_totals = dict(source_totals)
    for difference in rollforward_differences:
        for side in ('dr', 'cr'):
            adjusted_totals['closing_' + side + '_column'] += Decimal(difference['closing_' + side + '_adjustment_exact'])
    leaf_adjusted_totals = dict(adjusted_totals)
    global_adjustments = []
    if not errors:
        # Each opening/movement counter-entry carries its net effect into closing.
        # A final closing control catches accumulated tolerated row-rounding differences.
        for period, label in (('opening', 'Opening'), ('period', 'Period movement'), ('closing', 'Closing rounding')):
            net = adjusted_totals[period + '_dr_column'] - adjusted_totals[period + '_cr_column']
            if abs(net) <= tolerance:
                continue
            side = 'cr' if net > 0 else 'dr'
            values = {role: Decimal('0') for role, _ in amount_roles}
            values[period + '_' + side + '_column'] = abs(net)
            if period != 'closing':
                values['closing_' + side + '_column'] = abs(net)
            global_adjustments.append({'account_name': label + ' footing', 'global_adjustment': True,
                                       **{role.replace('_column', '_adjustment'): str(value) for role, value in values.items()}})
            for role, value in values.items():
                adjusted_totals[role] += value
    # Amount differences are advisory: keep their separate entries and observations
    # while the earlier source-total, account-code and input checks remain blocking.
    if global_adjustments:
        add_warning(None, 'Footings', 'Vertical debit/credit differences are recorded as separate Difference lines; original amounts remain unchanged.')
    for period, label in (('opening', 'Opening'), ('period', 'Period movement'), ('closing', 'Closing')):
        debit, credit = adjusted_totals[period + '_dr_column'], adjusted_totals[period + '_cr_column']
        checks.append({'key': period + '_balance', 'label': label + ' debits equal credits',
                       'status': 'blocked' if errors else ('adjusted' if any(
                           Decimal(row[period + '_' + side + '_adjustment']) != 0
                           for row in global_adjustments for side in ('dr', 'cr')) else 'pass'),
                       'debit_total': float(debit), 'credit_total': float(credit), 'difference': float(debit-credit),
                       'original_debit_total': str(source_totals[period + '_dr_column']),
                       'original_credit_total': str(source_totals[period + '_cr_column']),
                       'leaf_adjusted_debit_total': str(leaf_adjusted_totals[period + '_dr_column']),
                       'leaf_adjusted_credit_total': str(leaf_adjusted_totals[period + '_cr_column'])})

    error_count = len(errors)
    return {
        'valid': error_count == 0,
        'errors': errors,
        'warnings': warnings,
        'checks': checks,
        'account_classifications': sorted((r['account_code'], r['classification']) for r in candidates),
        'total_excel_row': next(iter(control_row_numbers), None),
        'no_total_row': bool(no_total_row),
        'source_total_rows': sorted(control_row_numbers),
        'source_total_comparison': source_comparison,
        'source_leaf_totals': {key: str(value) for key, value in source_totals.items()},
        'erp_unbalanced': _erp_totals_unbalanced(control_rows[0]['amounts'] if control_rows else None, tolerance),
        'draft_reconciliation': _build_draft_reconciliation(source_totals, adjusted_totals, tolerance) if not errors else None,
        'global_adjustments': global_adjustments,
        'global_adjustments_automatic': bool(global_adjustments),
        'rollforward_differences': rollforward_differences,
        'rollforward_difference_count': len(rollforward_differences),
        'account_count': len(candidates),
        'leaf_count': len(valid_amount_rows),
        'parent_count': len(candidates) - len(valid_amount_rows),
        'auto_classification': {str(row['excel_row']): row['auto_classification'] for row in candidates},
        'classification_overrides': {str(row_number): value for row_number, value in classification_overrides.items()},
        'excluded_subtotal_rows': sorted(excluded_excel_rows),
        'excluded_subtotal_count': len(excluded_excel_rows),
        'issue_count': error_count,
        'warning_count': len(warnings),
        'omitted_issue_count': 0,
        'tolerance': 0.01,
    }


def calculate_rollforward_adjustment(amounts, tolerance=Decimal('0.01')):
    """Return the closing-side adjustment that reconciles one account's roll-forward."""
    opening_net = amounts['opening_dr_column'] - amounts['opening_cr_column']
    movement_net = amounts['period_dr_column'] - amounts['period_cr_column']
    original_closing_net = amounts['closing_dr_column'] - amounts['closing_cr_column']
    expected_closing_net = opening_net + movement_net
    difference_net = expected_closing_net - original_closing_net
    if abs(difference_net) <= tolerance:
        return None
    return {
        'expected_closing_net': expected_closing_net,
        'original_closing_net': original_closing_net,
        'difference_net': difference_net,
        'closing_dr_adjustment': difference_net if difference_net > 0 else Decimal('0'),
        'closing_cr_adjustment': -difference_net if difference_net < 0 else Decimal('0'),
    }


def find_conflicting_uploads(client_id: int, period_start: str, period_end: str) -> list:
    """
    Section 2, Phase 5: shared exact-period conflict lookup, used by both the
    standalone check endpoint and the upload endpoint's own confirm_overwrite
    gate below - single source of truth so the two can never disagree.

    Exact match only on client_id + period_start + period_end (NOT an overlap
    check) - a different-but-overlapping period, e.g. a monthly TB inside an
    already-uploaded quarterly one, is never a conflict, per the locked scope.
    Matches an existing upload regardless of its status (parsed/error/pending)
    - the schema draws no distinction, and "an upload already exists for this
    exact period" is a simpler, more predictable rule than special-casing a
    prior failed attempt. Returns the raw row list (most recent first) since
    more than one conflicting row can exist (e.g. repeated failed attempts at
    the same period).
    """
    return execute_query_pg(
        """
        SELECT * FROM trial_balance_uploads
        WHERE client_id = %s AND period_start = %s AND period_end = %s
        ORDER BY uploaded_at DESC
        """,
        (client_id, period_start, period_end)
    )


def _validate_period_dates(period_start: str, period_end: str) -> None:
    """
    Shared ISO-date + ordering validation for period_start/period_end, used
    by both the check endpoint and the upload endpoint - same strptime
    pattern already used for as_of_date elsewhere in this file.
    """
    try:
        start_dt = datetime.strptime(period_start, "%Y-%m-%d")
    except ValueError:
        raise ValueError(f"'{period_start}' is not a valid period_start date (expected YYYY-MM-DD)")
    try:
        end_dt = datetime.strptime(period_end, "%Y-%m-%d")
    except ValueError:
        raise ValueError(f"'{period_end}' is not a valid period_end date (expected YYYY-MM-DD)")
    if end_dt < start_dt:
        raise ValueError(f"period_end ({period_end}) cannot be before period_start ({period_start})")


def _serialize_conflicting_upload(row) -> dict:
    """Shared response shape for a conflicting upload row, reused by both the
    check endpoint's 'existing_upload' and the upload endpoint's 409 detail,
    so the frontend (Phase 6) can render both with one function."""
    return {
        "upload_id": row["id"],
        "as_of_date": row["as_of_date"],
        "period_start": row["period_start"],
        "period_end": row["period_end"],
        "period_type": row["period_type"],
        "file_name": row["file_name"],
        "status": row["status"],
        "row_count": row["row_count"],
        "uploaded_at": row["uploaded_at"],
    }


def _account_code_path_key(code: str) -> tuple[str, ...]:
    """Normalize numeric segment padding for hierarchy matching only."""
    parts = str(code or '').strip().split('-')
    return tuple(
        (part.lstrip('0') or '0') if part.isascii() and part.isdigit() else part
        for part in parts
    )


def build_account_code_hierarchy(codes) -> dict:
    """Return classifications and nearest present parent codes for one TB."""
    code_set = {str(code).strip() for code in codes if str(code or '').strip()}
    codes_by_path = {}
    for code in code_set:
        codes_by_path.setdefault(_account_code_path_key(code), []).append(code)

    parent_by_code = {}
    for code in code_set:
        parts = code.split('-')
        parent = None
        for length in range(len(parts) - 1, 0, -1):
            raw_prefix = '-'.join(parts[:length])
            if raw_prefix in code_set:
                parent = raw_prefix
                break
            matches = codes_by_path.get(_account_code_path_key(raw_prefix), ())
            # A normalized match is useful only when it identifies one real row.
            if len(matches) == 1:
                parent = matches[0]
                break
        parent_by_code[code] = parent

    classification = {code: 'leaf' for code in code_set}
    for code in code_set:
        parent = parent_by_code[code]
        visited = set()
        while parent and parent not in visited:
            classification[parent] = 'parent'
            visited.add(parent)
            parent = parent_by_code.get(parent)

    return {
        'classification': classification,
        'parent_account_code': parent_by_code,
    }


def classify_accounts(codes: set) -> dict:
    """Classify codes from their delimiter-separated account-code hierarchy."""
    return build_account_code_hierarchy(codes)['classification']


def _copy_shared_code_assignments(client_id: int, source_code: str, target_code: str, upload_id: Optional[int] = None) -> None:
    """Carry shared category and ACC mappings to a corrected code without overwriting."""
    mappings = (
        ("account_category_assignments", ("category_id",)),
        ("account_control_assignments", (
            "verification_workstream", "financial_statement_category_id", "note_category_id",
        )),
        ("account_control_cashflow_assignments", (
            "section", "category_label", "cash_bank_role", "treatment_type",
        )),
        ("account_control_cashflow_category_assignments", ("category_id",)),
    )
    for table, value_columns in mappings:
        columns = ", ".join(value_columns)
        source_rows = execute_query_pg(
            f"SELECT {columns} FROM {table} WHERE client_id = %s AND account_code = %s",
            (client_id, source_code),
        )
        target_rows = execute_query_pg(
            f"SELECT {columns} FROM {table} WHERE client_id = %s AND account_code = %s",
            (client_id, target_code),
        )
        if not source_rows:
            continue
        def has_assignment(row):
            if table == "account_control_cashflow_assignments":
                return bool(
                    row.get("section") not in (None, "unassigned")
                    or row.get("category_label")
                    or row.get("cash_bank_role") not in (None, "none")
                    or row.get("treatment_type") not in (None, "standard")
                )
            return any(row.get(column) is not None for column in value_columns)

        source_values = tuple(source_rows[0].get(c) for c in value_columns)
        if not has_assignment(source_rows[0]):
            continue
        if target_rows:
            target_values = tuple(target_rows[0].get(c) for c in value_columns)
            if source_values == target_values:
                continue
            if has_assignment(target_rows[0]):
                raise HTTPException(
                    409,
                    f"Account code {target_code} already has a different {table.replace('_', ' ')} assignment. "
                    "Resolve that assignment before correcting this code.",
                )
            # A saved Unassigned row is equivalent to no mapping. Copy the whole
            # assignment, preserving the FS/Note pair, only if it is still empty.
            updates = ', '.join(f'{column} = %s' for column in value_columns)
            updates += ", updated_at = CURRENT_TIMESTAMP"
            if table != "account_category_assignments":
                updates += ", updated_by = 'Analyst'"
            unchanged = ' AND '.join(f'{column} IS NOT DISTINCT FROM %s' for column in value_columns)
            copied = execute_write_pg(
                f"UPDATE {table} SET {updates} WHERE client_id = %s AND account_code = %s "
                f"AND {unchanged} RETURNING account_code",
                (*source_values, client_id, target_code, *target_values),
            )
            if copied is None:
                raise HTTPException(409, f"Assignments for {target_code} changed while correcting the code. Reload and retry.")
            continue
        insert_columns = ["client_id", "account_code", *value_columns]
        if table != "account_category_assignments":
            insert_columns.extend(("updated_by", "updated_at"))
        placeholders = ", ".join(["%s"] * (2 + len(value_columns)))
        if table != "account_category_assignments":
            placeholders += ", 'Analyst', CURRENT_TIMESTAMP"
        execute_write_pg(
            f"INSERT INTO {table} ({', '.join(insert_columns)}) VALUES ({placeholders})",
            (client_id, target_code, *(source_rows[0].get(c) for c in value_columns)),
        )

    for table, key_columns, value_columns in (
        ("account_groups", ("group_label",), ("is_active",)),
        ("report_account_category_assignments", ("category_set_id",), ("category_id",)),
        ("account_control_cashflow_treasury_accounts", ("treasury_type",), ("is_active",)),
    ):
        columns = [*key_columns, *value_columns]
        source_rows = execute_query_pg(
            f"SELECT {', '.join(columns)} FROM {table} WHERE client_id = %s AND account_code = %s",
            (client_id, source_code),
        )
        for source in source_rows:
            key_where = " AND ".join(f"{column} = %s" for column in key_columns)
            key_values = tuple(source[column] for column in key_columns)
            target_rows = execute_query_pg(
                f"SELECT {', '.join(value_columns)} FROM {table} "
                f"WHERE client_id = %s AND account_code = %s AND {key_where}",
                (client_id, target_code, *key_values),
            )
            source_values = tuple(source[column] for column in value_columns)
            if target_rows:
                if tuple(target_rows[0][column] for column in value_columns) != source_values:
                    raise HTTPException(409, f"Account code {target_code} has a conflicting {table.replace('_', ' ')} assignment.")
                continue
            insert_columns = ["client_id", "account_code", *columns]
            values = [client_id, target_code, *(source[column] for column in columns)]
            placeholders = ", ".join(["%s"] * len(insert_columns))
            if table != "account_groups":
                insert_columns.append("updated_at")
                placeholders += ", CURRENT_TIMESTAMP"
            if table == "account_control_cashflow_treasury_accounts":
                insert_columns.append("updated_by")
                placeholders += ", 'Analyst'"
            execute_write_pg(
                f"INSERT INTO {table} ({', '.join(insert_columns)}) VALUES ({placeholders})",
                tuple(values),
            )

    if upload_id is not None:
        selector_rows = execute_query_pg(
            """SELECT s.id, s.account_set_version_id, s.selector_type
               FROM account_set_selectors s
               JOIN account_set_versions v ON v.id = s.account_set_version_id
               WHERE v.source_upload_id = %s AND s.account_code = %s""",
            (upload_id, source_code),
        )
        for selector in selector_rows:
            conflict = execute_query_pg(
                """SELECT id FROM account_set_selectors
                   WHERE account_set_version_id = %s AND selector_type = %s AND account_code = %s""",
                (selector["account_set_version_id"], selector["selector_type"], target_code),
            )
            if conflict:
                raise HTTPException(409, f"Account code {target_code} is already in an account set for this upload.")
            execute_write_pg("UPDATE account_set_selectors SET account_code = %s WHERE id = %s", (target_code, selector["id"]))


@router.post("/api/trial-balance/validate")
def validate_trial_balance_upload(
    file: UploadFile = File(...),
    account_code_column: Optional[str] = Form(None),
    account_name_column: Optional[str] = Form(None),
    type_column: Optional[str] = Form(None),
    opening_dr_column: Optional[str] = Form(None),
    opening_cr_column: Optional[str] = Form(None),
    period_dr_column: Optional[str] = Form(None),
    period_cr_column: Optional[str] = Form(None),
    closing_dr_column: Optional[str] = Form(None),
    closing_cr_column: Optional[str] = Form(None),
    account_code_overrides: Optional[str] = Form(None),
    account_classification_overrides: Optional[str] = Form(None),
    excluded_excel_rows: Optional[str] = Form(None),
    total_excel_row: Optional[str] = Form(None),
    no_total_row: bool = Form(False),
):
    """Validate a Trial Balance without creating or replacing an upload."""
    if not file.filename or not file.filename.lower().endswith(('.xlsx', '.xls')):
        raise HTTPException(400, "Only .xlsx and .xls files are supported")
    mapping = {
        "account_code_column": account_code_column,
        "account_name_column": account_name_column,
        "type_column": type_column,
        "opening_dr_column": opening_dr_column,
        "opening_cr_column": opening_cr_column,
        "period_dr_column": period_dr_column,
        "period_cr_column": period_cr_column,
        "closing_dr_column": closing_dr_column,
        "closing_cr_column": closing_cr_column,
    }
    try:
        return validate_trial_balance_contents(
            file.file.read(), file.filename, mapping,
            account_code_overrides, account_classification_overrides, excluded_excel_rows, total_excel_row, no_total_row,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@router.post("/api/trial-balance/upload")
def upload_trial_balance(
    client_id: int = Form(...),
    as_of_date: str = Form(...),
    file: UploadFile = File(...),
    # Column mapping (Section 1, Phase 1) - which real Excel column is which
    # field. Required-by-business-rule fields are still Optional[str] at the
    # FastAPI layer (not Form(...)) so validate_column_mapping() below can
    # produce one combined, clear 400 naming every missing/invalid field at
    # once, rather than FastAPI's generic 422 on the first missing field.
    account_code_column: Optional[str] = Form(None),
    account_name_column: Optional[str] = Form(None),
    type_column: Optional[str] = Form(None),
    opening_dr_column: Optional[str] = Form(None),
    opening_cr_column: Optional[str] = Form(None),
    period_dr_column: Optional[str] = Form(None),
    period_cr_column: Optional[str] = Form(None),
    closing_dr_column: Optional[str] = Form(None),
    closing_cr_column: Optional[str] = Form(None),
    # Section 2, Phase 5: optional period tracking + overwrite confirmation.
    # All optional and backward-compatible - omitting period_start/period_end
    # entirely reproduces this endpoint's exact pre-Phase-5 behavior (no
    # conflict check runs at all).
    period_start: Optional[str] = Form(None),
    period_end: Optional[str] = Form(None),
    period_type: Optional[str] = Form(None),
    confirm_overwrite: bool = Form(False),
    # Fast Trial Balance upload: optional client-generated key for the frontend's progress
    # poll (GET /api/trial-balance/upload-status/{progress_key}) - see TB_UPLOAD_PROGRESS
    # above. Entirely optional, so a caller omitting it (e.g. a test) sees no behavior change.
    progress_key: Optional[str] = Form(None),
    account_code_overrides: Optional[str] = Form(None),
    account_classification_overrides: Optional[str] = Form(None),
    excluded_excel_rows: Optional[str] = Form(None),
    total_excel_row: Optional[str] = Form(None),
    no_total_row: bool = Form(False),
):
    """
    Save a Trial Balance only after the same non-writing validation used by
    the review screen has passed; classification and optional overrides are
    stored with the accepted upload.
    """
    # 1. Client must exist and be active.
    client_rows = execute_query_pg("SELECT id FROM clients WHERE id = %s AND is_active = TRUE", (client_id,))
    if not client_rows:
        raise HTTPException(404, "Client not found")

    # 2. as_of_date must be a real ISO date.
    try:
        datetime.strptime(as_of_date, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(400, f"'{as_of_date}' is not a valid date (expected YYYY-MM-DD)")

    # 3. Extension check - same two extensions import_excel() already accepts.
    if not file.filename or not file.filename.lower().endswith(('.xlsx', '.xls')):
        raise HTTPException(400, "Only .xlsx and .xls files are supported")

    # 3b. Section 2, Phase 5: period validation + exact-period overwrite gate.
    # Both period_start/period_end must be given together, or neither.
    if bool(period_start) != bool(period_end):
        raise HTTPException(400, "period_start and period_end must both be provided together, or neither.")

    conflicts = []
    if period_start and period_end:
        try:
            _validate_period_dates(period_start, period_end)
        except ValueError as e:
            raise HTTPException(400, str(e))

        conflicts = find_conflicting_uploads(client_id, period_start, period_end)
        if conflicts:
            if not confirm_overwrite:
                # Reject before anything is created or touched - this is what
                # protects against Phase 6's UI being skipped or a stray API
                # call proceeding blindly, per the confirmed brief.
                raise HTTPException(409, detail={
                    "conflict": True,
                    "message": (
                        f"An upload already exists for this client for the exact period "
                        f"{period_start} to {period_end}. Re-upload with confirm_overwrite=true "
                        f"to replace it."
                    ),
                    "existing_upload": _serialize_conflicting_upload(conflicts[0]),
                    "conflicting_upload_count": len(conflicts),
                })

            # Defer deletion until parsing, code corrections, and assignment-conflict
            # checks all pass. Replacement and new rows are committed together below.

    mapping = {
        "account_code_column": account_code_column,
        "account_name_column": account_name_column,
        "type_column": type_column,
        "opening_dr_column": opening_dr_column,
        "opening_cr_column": opening_cr_column,
        "period_dr_column": period_dr_column,
        "period_cr_column": period_cr_column,
        "closing_dr_column": closing_dr_column,
        "closing_cr_column": closing_cr_column,
    }
    try:
        validation = validate_trial_balance_contents(
            file.file.read(), file.filename, mapping,
            account_code_overrides, account_classification_overrides, excluded_excel_rows, total_excel_row, no_total_row,
        )
    except ValueError as exc:
        raise HTTPException(400, f"Trial balance validation failed: {exc}")
    if not validation['valid']:
        raise HTTPException(400, detail={
            'message': 'Trial Balance validation failed. Resolve the listed issues before saving.',
            'validation': validation,
        })
    file.file.seek(0)

    # 4. Create the upload row immediately (status='pending') so even a
    # failed parse below leaves a visible, queryable record rather than
    # silently vanishing.
    upload_id = execute_write_pg(
        """
        INSERT INTO trial_balance_uploads
            (client_id, as_of_date, file_name, status, period_start, period_end, period_type)
        VALUES (%s, %s, %s, 'pending', %s, %s, %s)
        RETURNING id
        """,
        (client_id, as_of_date, file.filename, period_start, period_end, period_type)
    )

    try:
        # Fast Trial Balance upload: synchronous read (UploadFile.file is a real
        # SpooledTemporaryFile) - required now that this endpoint is a plain `def`, run in
        # FastAPI's threadpool so a concurrent progress poll can actually be served while
        # this request is still in flight (an `async def` here would block the event loop).
        contents = file.file.read()
        df = read_trial_balance_excel(contents, file.filename)

        # Validate the caller-supplied mapping against the file's real headers
        # (Section 1, Phase 1) - replaces the old hardcoded identify_columns()
        # auto-detection. A ValueError here (missing required role, or a
        # mapped column that isn't a real header) is caught by the except
        # block below, same as any other parsing failure.
        mapping = {
            "account_code_column": account_code_column,
            "account_name_column": account_name_column,
            "type_column": type_column,
            "opening_dr_column": opening_dr_column,
            "opening_cr_column": opening_cr_column,
            "period_dr_column": period_dr_column,
            "period_cr_column": period_cr_column,
            "closing_dr_column": closing_dr_column,
            "closing_cr_column": closing_cr_column,
        }
        validate_column_mapping(mapping, df.columns.tolist())
        rows = df.to_dict('records')
        try:
            submitted_overrides = json.loads(account_code_overrides or "{}")
        except json.JSONDecodeError:
            raise ValueError("Account code corrections were invalid. Reopen the file review and try again.")
        if not isinstance(submitted_overrides, dict):
            raise ValueError("Account code corrections must be supplied by Excel row number.")
        code_overrides = {}
        for row_key, corrected_code in submitted_overrides.items():
            try:
                row_number = int(row_key)
            except (TypeError, ValueError):
                raise ValueError("An account code correction had an invalid Excel row number.")
            if row_number < 2 or row_number > len(rows) + 1:
                raise ValueError(f"Account code correction row {row_number} is outside the uploaded file.")
            corrected_code = str(corrected_code or "").strip()
            if not corrected_code:
                raise ValueError(f"Account code on Excel row {row_number} cannot be blank.")
            code_overrides[row_number] = corrected_code

        # Fast Trial Balance upload: row-parse progress tracking, mirroring
        # import_export.py's IMPORT_PROGRESS. Increments during this loop (fast, in-memory),
        # not during the network-bound batched INSERT further below - same caveat Data
        # Bank's own progress bar already has, not a new gap.
        if progress_key:
            TB_UPLOAD_PROGRESS[progress_key] = {"current": 0, "total": len(rows), "status": "processing"}

        # Walk every row once: skip blank codes, reject duplicates, and capture the rest.
        # sort_order uses the same "header=1, first data row=2" Excel row
        # numbering convention already established elsewhere in this app.
        seen_codes = set()
        accounts = []  # list of dicts, pre-classification
        skipped_blank_count = 0
        skipped_subtotal_count = len(validation['excluded_subtotal_rows'])
        excluded_subtotal_rows = set(validation['excluded_subtotal_rows']) | set(validation['source_total_rows'])

        for idx, row in enumerate(rows):
            if progress_key:
                TB_UPLOAD_PROGRESS[progress_key]["current"] = idx + 1
            row_number = idx + 2
            source_code = str(row.get(account_code_column, '')).strip()
            code = code_overrides.get(row_number, source_code)
            if row_number in excluded_subtotal_rows:
                continue
            if code == '':
                skipped_blank_count += 1
                continue
            if code in seen_codes:
                raise ValueError(
                    f"Account code '{code}' appears more than once after corrections (including Excel row {row_number}). "
                    "Edit the codes so every account code is unique, then upload again."
                )
            seen_codes.add(code)

            name_value = str(row.get(account_name_column, '')).strip() if account_name_column else ''
            type_value = str(row.get(type_column, '')).strip() if type_column else ''
            opening_dr_value = parse_amount(row.get(opening_dr_column)) if opening_dr_column else None
            opening_cr_value = parse_amount(row.get(opening_cr_column)) if opening_cr_column else None
            period_dr_value = parse_amount(row.get(period_dr_column)) if period_dr_column else None
            period_cr_value = parse_amount(row.get(period_cr_column)) if period_cr_column else None
            closing_dr_value = parse_amount(row.get(closing_dr_column)) if closing_dr_column else None
            closing_cr_value = parse_amount(row.get(closing_cr_column)) if closing_cr_column else None

            accounts.append({
                "account_code": code,
                "source_account_code": source_code,
                "account_name": name_value or None,
                "source_account_type": type_value or None,
                "opening_dr": opening_dr_value,
                "opening_cr": opening_cr_value,
                "period_dr": period_dr_value,
                "period_cr": period_cr_value,
                "closing_dr": closing_dr_value,
                "closing_cr": closing_cr_value,
                "sort_order": row_number,
                # Keep a raw-value safety net for mapped source fields only.
                # Unmapped workbook columns are preview-only and are not imported.
                # Json(...) leaves encoding to psycopg2/Postgres for JSONB.
                "raw_row_data": Json(_mapped_raw_row_data(row, mapping)),
            })

        if not accounts:
            raise ValueError("No valid account rows found in the file (every row had a blank Account Code)")

        # 8. Classify every distinct code from its dash-delimited hierarchy.
        hierarchy = build_account_code_hierarchy(seen_codes)
        classification = hierarchy['classification']
        parent_account_codes = hierarchy['parent_account_code']
        classification_overrides = validation['classification_overrides']

        # 8b. Carry-forward (Verification link, Phase 2): look up this
        # client's persistent account_category_assignments once (a single
        # query, not one per account) and pre-populate category_id for every
        # account code that already has a saved assignment. A code with no
        # match is simply left unassigned (category_id stays None) - that's
        # the "genuinely new account code" case the brief calls out.
        assignment_rows = execute_query_pg(
            "SELECT account_code, category_id FROM account_category_assignments WHERE client_id = %s",
            (client_id,)
        )
        carried_categories = {r["account_code"]: r["category_id"] for r in assignment_rows}

        for account in accounts:
            code = account["account_code"]
            account["level"] = len(code.split('-'))
            account["parent_account_code"] = parent_account_codes.get(code)
            account["classification"] = classification_overrides.get(str(account["sort_order"]), classification[code])
            account["manually_overridden"] = str(account["sort_order"]) in classification_overrides
            account["category_id"] = carried_categories.get(code)
            if account["category_id"] is None and code != account["source_account_code"]:
                account["category_id"] = carried_categories.get(account["source_account_code"])

        # Summarize blank-code rows skipped from the source file.
        notes_parts = []
        if skipped_blank_count:
            notes_parts.append(f"{skipped_blank_count} blank row(s) skipped")
        if skipped_subtotal_count:
            notes_parts.append(f"{skipped_subtotal_count} source subtotal row(s) excluded")
        notes = "; ".join(notes_parts) if notes_parts else None

        # 9. Replace and insert in one transaction, so a parse/duplicate/mapping error
        # never removes the previously confirmed period upload.
        # instead of one INSERT round-trip per account row, same fix already proven for
        # Data Bank's own import (routers/import_export.py). execute_batch_pg() commits/
        # rolls back per chunk internally, so no separate outer try/except is needed here -
        # this function's own outer except Exception (below) still catches any failure and
        # marks the upload status='error' exactly as before.
        with pg_transaction():
            for old_upload in conflicts:
                execute_write_pg("DELETE FROM trial_balance_rollforward_adjustments WHERE upload_id = %s", (old_upload["id"],))
                execute_write_pg("DELETE FROM trial_balance_accounts WHERE upload_id = %s", (old_upload["id"],))
                execute_write_pg("DELETE FROM trial_balance_uploads WHERE id = %s", (old_upload["id"],))
            for account in accounts:
                if account["source_account_code"] != account["account_code"]:
                    _copy_shared_code_assignments(client_id, account["source_account_code"], account["account_code"], upload_id)
            conn = get_raw_conn_pg()
            try:
                params_list = [
                    (upload_id, account["account_code"], account["source_account_code"], account["account_name"],
                     account["parent_account_code"], account["level"], account["classification"],
                     account["opening_dr"], account["opening_cr"], account["period_dr"], account["period_cr"],
                     account["closing_dr"], account["closing_cr"], account["source_account_type"],
                     account["sort_order"], account["raw_row_data"], account["category_id"],
                     account["manually_overridden"])
                    for account in accounts
                ]
                execute_batch_pg(
                    conn,
                    """
                    INSERT INTO trial_balance_accounts
                        (upload_id, account_code, source_account_code, account_name, parent_account_code, level,
                         classification, opening_dr, opening_cr, period_dr, period_cr,
                         closing_dr, closing_cr, source_account_type, sort_order, raw_row_data,
                         category_id, manually_overridden)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    params_list
                )
            finally:
                release_raw_conn_pg(conn)
            if validation['rollforward_differences']:
                account_ids_by_row = {
                    row['sort_order']: row['id']
                    for row in execute_query_pg(
                        "SELECT id, sort_order FROM trial_balance_accounts WHERE upload_id = %s",
                        (upload_id,),
                    )
                }
                adjustment_params = []
                for difference in validation['rollforward_differences']:
                    account_id = account_ids_by_row.get(difference['excel_row'])
                    if account_id is None:
                        raise ValueError(f"Could not find the leaf account for Excel row {difference['excel_row']}.")
                    adjustment_params.append((
                        upload_id,
                        account_id,
                        difference['excel_row'],
                        difference['expected_closing_net_exact'],
                        difference['original_closing_net_exact'],
                        difference['difference_net_exact'],
                        difference['closing_dr_adjustment_exact'],
                        difference['closing_cr_adjustment_exact'],
                    ))
                adjustment_conn = get_raw_conn_pg()
                try:
                    execute_batch_pg(
                        adjustment_conn,
                        """
                        INSERT INTO trial_balance_rollforward_adjustments
                            (upload_id, account_id, source_excel_row, expected_closing_net,
                             original_closing_net, difference_net, closing_dr_adjustment,
                             closing_cr_adjustment)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                        """,
                        adjustment_params,
                    )
                finally:
                    release_raw_conn_pg(adjustment_conn)
            execute_write_pg(
                "UPDATE trial_balance_uploads SET status = 'parsed', row_count = %s, notes = %s, validation_controls = %s WHERE id = %s",
                (len(accounts), notes, json.dumps({
                    'total_excel_row': validation['total_excel_row'],
                    'no_total_row': validation['no_total_row'],
                    'source_total_rows': validation['source_total_rows'],
                    'source_total_comparison': validation['source_total_comparison'],
                    'checks': validation['checks'], 'global_adjustments': validation['global_adjustments'],
                    'account_classifications': validation['account_classifications'],
                    'global_adjustments_automatic': validation['global_adjustments_automatic'],
                    'erp_unbalanced': validation['erp_unbalanced'],
                    'draft_reconciliation': validation['draft_reconciliation'],
                }), upload_id)
            )
        if conflicts:
            log_audit("Analyst", "Delete", "TrialBalance",
                      f"Overwrote {len(conflicts)} prior trial balance upload(s) for client "
                      f"{client_id}, exact period {period_start} to {period_end}")
        invalidate_category_for_account_cache(client_id)

        parent_count = sum(1 for a in accounts if a["classification"] == "parent")
        leaf_count = len(accounts) - parent_count
        corrected_code_count = sum(1 for a in accounts if a["source_account_code"] != a["account_code"])
        rollforward_adjustment_count = len(validation['rollforward_differences'])

        log_audit("Analyst", "Upload", "TrialBalance",
                   f"Uploaded trial balance for client {client_id}: {len(accounts)} accounts "
                   f"({parent_count} parent, {leaf_count} leaf; {corrected_code_count} code correction(s); "
                   f"{rollforward_adjustment_count} roll-forward difference(s))")

        if progress_key:
            TB_UPLOAD_PROGRESS[progress_key]["status"] = "complete"

        return {
            "upload_id": upload_id,
            "row_count": len(accounts),
            "parent_count": parent_count,
            "leaf_count": leaf_count,
            "skipped_blank_count": skipped_blank_count,
            "duplicate_code_count": 0,
            "corrected_code_count": corrected_code_count,
            "rollforward_adjustment_count": rollforward_adjustment_count,
            "global_adjustment_count": len(validation["global_adjustments"]),
        }

    except HTTPException as e:
        execute_write_pg(
            "UPDATE trial_balance_uploads SET status = 'error', notes = %s WHERE id = %s",
            (str(e.detail), upload_id)
        )
        if progress_key:
            TB_UPLOAD_PROGRESS[progress_key]["status"] = "error"
        raise
    except Exception as e:
        # Leave the upload row visible with status='error' rather than
        # deleting it - the 'error' status + notes column exist for exactly
        # this case (Phase 1 schema).
        execute_write_pg(
            "UPDATE trial_balance_uploads SET status = 'error', notes = %s WHERE id = %s",
            (str(e), upload_id)
        )
        if progress_key:
            TB_UPLOAD_PROGRESS[progress_key]["status"] = "error"
        raise HTTPException(400, f"Trial balance parsing failed: {e}")


@router.get("/api/trial-balance/upload-status/{progress_key}")
def get_tb_upload_status(progress_key: str):
    """Polled by the frontend while POST /api/trial-balance/upload is in flight for this key."""
    return TB_UPLOAD_PROGRESS.get(progress_key, {"current": 0, "total": 0, "status": "idle"})


@router.get("/api/trial-balance/check-period-conflict")
def check_period_conflict(client_id: int, period_start: str, period_end: str):
    """
    Section 2, Phase 5: read-only "step 1" of the two-step overwrite flow -
    tells the frontend (Phase 6) whether an exact-period upload already
    exists for this client, before the user commits to an actual upload
    call. Touches no data. Exact match only (client_id + period_start +
    period_end) - see find_conflicting_uploads()'s own docstring for why an
    overlapping-but-different period is never flagged.
    """
    try:
        _validate_period_dates(period_start, period_end)
    except ValueError as e:
        raise HTTPException(400, str(e))

    conflicts = find_conflicting_uploads(client_id, period_start, period_end)
    if not conflicts:
        return {"conflict": False, "existing_upload": None}

    return {
        "conflict": True,
        "existing_upload": _serialize_conflicting_upload(conflicts[0]),
        "conflicting_upload_count": len(conflicts),
    }


@router.post("/api/trial-balance/suggest-mapping")
async def suggest_trial_balance_mapping(file: UploadFile = File(...)):
    """
    Section 1, Phase 1: stateless read-only endpoint for the frontend mapping
    window (Phase 2) - takes the same raw Excel file the user is about to
    upload, reads its headers, and returns a best-guess suggestion per
    mapping role so the UI can pre-fill likely matches. Nothing is stored;
    this never touches trial_balance_uploads/trial_balance_accounts.
    """
    if not file.filename or not file.filename.lower().endswith(('.xlsx', '.xls')):
        raise HTTPException(400, "Only .xlsx and .xls files are supported")

    try:
        contents = await file.read()
        df = read_trial_balance_excel(contents, file.filename)
    except ValueError as e:
        raise HTTPException(400, str(e))

    headers = df.columns.tolist()
    return {
        "headers": headers,
        "suggestions": suggest_column_mapping(headers),
    }


@router.get("/api/trial-balance/saved-column-mapping")
def get_saved_trial_balance_column_mapping(
    client_id: int = Query(..., ge=1),
    session=Depends(require_admin),
):
    """Return the single saved Trial Balance column map for this client, if any."""
    rows = execute_query_pg(
        "SELECT mapping_json FROM trial_balance_column_mappings WHERE client_id = %s",
        (client_id,),
    )
    if not rows:
        return {"mapping": None}
    mapping = rows[0]["mapping_json"]
    if isinstance(mapping, str):
        try:
            mapping = json.loads(mapping)
        except json.JSONDecodeError:
            raise HTTPException(500, "The saved Trial Balance mapping is unreadable.")
    return {"mapping": mapping}


@router.put("/api/trial-balance/saved-column-mapping")
def save_trial_balance_column_mapping(
    payload: TrialBalanceColumnMappingPayload,
    session=Depends(require_admin),
):
    """Create or replace the one explicitly saved Trial Balance map for a client."""
    expected_roles = set(REQUIRED_MAPPING_ROLES)
    if set(payload.mapping) != expected_roles:
        raise HTTPException(400, "Provide a mapping for all eight required Trial Balance fields.")
    values = [payload.mapping.get(role) for role in REQUIRED_MAPPING_ROLES]
    if any(not isinstance(value, str) or not value.strip() for value in values):
        raise HTTPException(400, "All eight Trial Balance fields must be mapped before saving.")
    if len(values) != len(set(values)):
        raise HTTPException(400, "Each Trial Balance field must use a different Excel column.")
    clients = execute_query_pg(
        "SELECT id FROM clients WHERE id = %s AND is_active = TRUE",
        (payload.client_id,),
    )
    if not clients:
        raise HTTPException(404, "Active client not found.")

    execute_write_pg(
        """
        INSERT INTO trial_balance_column_mappings (client_id, mapping_json)
        VALUES (%s, %s)
        ON CONFLICT (client_id) DO UPDATE
        SET mapping_json = EXCLUDED.mapping_json, updated_at = now()
        """,
        (payload.client_id, Json(payload.mapping)),
    )
    return {"saved": True, "client_id": payload.client_id, "mapping": payload.mapping}


@router.get("/api/trial-balance/download-template")
def download_trial_balance_template():
    """
    Section 1, Phase 3: escape-hatch downloadable standard template for a source
    file too irregular for the mapping window to handle cleanly (e.g. ambiguous
    repeated headers). Just the 8 standard headers, no sample/data rows, no
    per-client scoping - a file built from this still goes through the exact
    same suggest-mapping/upload flow as any other file (no separate "trusted
    format" path), per the locked scope.
    """
    output = BytesIO()
    df = pd.DataFrame(columns=STANDARD_TEMPLATE_HEADERS)
    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        df.to_excel(writer, index=False, sheet_name='Trial Balance')
    output.seek(0)

    return StreamingResponse(
        output,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename=trial_balance_standard_template.xlsx"}
    )


def _require_upload(upload_id: int):
    """
    Shared existence check for the two read endpoints below - 404s if the
    given upload_id doesn't exist in trial_balance_uploads at all. An upload
    that exists but has zero accounts (e.g. it errored during Phase 2, or
    genuinely had none) is NOT an error here - it just returns an empty
    list / all-zero counts, since that's a legitimate state, not a bad request.
    """
    upload_rows = execute_query_pg("SELECT id, validation_controls FROM trial_balance_uploads WHERE id = %s", (upload_id,))
    if not upload_rows:
        raise HTTPException(404, "Trial balance upload not found")
    return json.loads(upload_rows[0].get("validation_controls") or "{}")


# ---------- RETRIEVAL ENDPOINTS (Phase 3 of 5: store + retrieve classified accounts) ----------
# Listing remains read-only; account-code and classification writes use their
# separate endpoints. Each list request is scoped to one upload_id.

@router.get("/api/trial-balance/accounts")
def list_trial_balance_accounts(upload_id: int, is_leaf: Optional[bool] = None):
    """
    List one upload's classified accounts, in original file order. Optional
    is_leaf filter narrows to ledger-only (true) or roll-up-only (false)
    accounts, so the Upload UI (Phase 4) and Review screen (Phase 5) can
    show either the full hierarchy or a ledger-only view.
    """
    controls = _require_upload(upload_id)
    controls = dict(controls) if isinstance(controls, dict) else {}

    query = """SELECT id, account_code, source_account_code, account_name, level, classification, manually_overridden,
                      opening_dr, opening_cr, period_dr, period_cr, closing_dr, closing_cr
               FROM trial_balance_accounts WHERE upload_id = %s"""
    query += " ORDER BY sort_order"

    rows = execute_query_pg(query, (upload_id,))
    adjustment_rows = execute_query_pg(
        """
        SELECT a.id AS account_id, a.account_code, a.account_name, x.source_excel_row,
               x.expected_closing_net, x.original_closing_net, x.difference_net,
               x.closing_dr_adjustment, x.closing_cr_adjustment
        FROM trial_balance_rollforward_adjustments x
        JOIN trial_balance_accounts a ON a.id = x.account_id
        WHERE x.upload_id = %s
        ORDER BY x.source_excel_row
        """,
        (upload_id,),
    )
    auto_classification = classify_accounts({row['account_code'] for row in rows})
    accounts = []
    for r in rows:
        classification = r['classification'] if r['manually_overridden'] else auto_classification[r['account_code']]
        account_is_leaf = classification == 'leaf'
        if is_leaf is not None and account_is_leaf != is_leaf:
            continue
        accounts.append({
            "id": r["id"],
            "account_code": r["account_code"],
            "source_account_code": r["source_account_code"],
            "account_name": r["account_name"],
            "level": r["level"],
            "is_leaf": account_is_leaf,
            "manually_overridden": bool(r["manually_overridden"]),
            "opening_dr": _saved_trial_balance_amount(r["opening_dr"]),
            "opening_cr": _saved_trial_balance_amount(r["opening_cr"]),
            "period_dr": _saved_trial_balance_amount(r["period_dr"]),
            "period_cr": _saved_trial_balance_amount(r["period_cr"]),
            "closing_dr": _saved_trial_balance_amount(r["closing_dr"]),
            "closing_cr": _saved_trial_balance_amount(r["closing_cr"]),
        })

    current_classifications = sorted([r['account_code'], r['classification'] if r['manually_overridden'] else auto_classification[r['account_code']]] for r in rows)
    if controls.get('account_classifications') and current_classifications != controls['account_classifications']:
        controls['stale'] = True

    # A stale ERP validation does not make current stored figures unknowable.
    # Rebuild these read-only footings from the same rows returned above, never
    # from the visible filter or by creating fresh balancing adjustments.
    leaf_rows = [row for row in rows if (
        row['classification'] if row['manually_overridden'] else auto_classification[row['account_code']]) == 'leaf']
    leaf_ids = {row['id'] for row in leaf_rows}
    controls['current_reconciliation'], controls['current_reconciliation_error'] = _saved_trial_balance_reconciliation(
        leaf_rows, adjustment_rows, controls.get('global_adjustments', []))
    if controls.get('stale'):
        controls['draft_reconciliation'] = None
    elif not controls.get('draft_reconciliation'):
        controls['draft_reconciliation'] = controls['current_reconciliation']
    if 'erp_unbalanced' not in controls:
        comparisons = controls.get('source_total_comparison', [])
        labels = {period + '_' + side + '_column': label + ' ' + side.title()
                  for period, label in (('opening', 'Opening'), ('period', 'Period'), ('closing', 'Closing'))
                  for side in ('dr', 'cr')}
        try:
            by_label = {entry['field']: Decimal(entry['source_total']) for entry in comparisons}
            controls['erp_unbalanced'] = _erp_totals_unbalanced(
                {role: by_label[label] for role, label in labels.items()}
                if all(label in by_label and by_label[label].is_finite() for label in labels.values()) else None)
        except (InvalidOperation, ValueError, TypeError, KeyError):
            # Missing historic ERP evidence cannot invalidate current stored sums.
            controls['erp_unbalanced'] = None

    adjustments = [
        {
            "account_id": row["account_id"],
            "account_code": row["account_code"],
            "account_name": row["account_name"],
            "source_excel_row": row["source_excel_row"],
            "expected_closing_net": _saved_trial_balance_amount(row["expected_closing_net"], as_float=True),
            "original_closing_net": _saved_trial_balance_amount(row["original_closing_net"], as_float=True),
            "difference_net": _saved_trial_balance_amount(row["difference_net"], as_float=True),
            "closing_dr_adjustment": _saved_trial_balance_amount(row["closing_dr_adjustment"], as_float=True),
            "closing_cr_adjustment": _saved_trial_balance_amount(row["closing_cr_adjustment"], as_float=True),
            "included_in_footing": row['account_id'] in leaf_ids,
        }
        for row in adjustment_rows
    ]
    return {"upload_id": upload_id, "accounts": accounts, "adjustments": adjustments,
            "global_adjustments": controls.get("global_adjustments", []), "validation_controls": controls}


def _saved_trial_balance_amount(value, as_float=False):
    """Keep invalid saved values visible without invalid JSON numeric tokens."""
    try:
        number = float(value) if as_float else value
    except (ValueError, TypeError, OverflowError):
        return str(value) if value is not None else None
    if ((isinstance(number, float) and not math.isfinite(number))
            or (isinstance(number, Decimal) and not number.is_finite())):
        return str(value)
    return number


def _saved_trial_balance_reconciliation(leaf_rows, adjustment_rows, global_adjustments):
    """Sum current stored Leaves and applicable saved entries without writing."""
    roles = [period + '_' + side + '_column'
             for period in ('opening', 'period', 'closing') for side in ('dr', 'cr')]

    def amount(value):
        parsed = _parse_validation_amount(value)
        if parsed is None:
            raise ValueError('Invalid stored amount')
        return parsed

    try:
        original = {role: sum((amount(row[role.replace('_column', '')]) for row in leaf_rows), Decimal('0'))
                    for role in roles}
        adjusted = dict(original)
        leaf_ids = {row['id'] for row in leaf_rows}
        for entry in adjustment_rows:
            if entry['account_id'] in leaf_ids:
                for side in ('dr', 'cr'):
                    adjusted['closing_' + side + '_column'] += amount(entry['closing_' + side + '_adjustment'])
        for entry in global_adjustments:
            for role in roles:
                adjusted[role] += amount(entry.get(role.replace('_column', '_adjustment'), 0))
        return _build_draft_reconciliation(original, adjusted), None
    except (ValueError, InvalidOperation, TypeError, KeyError, AttributeError):
        return None, 'Current saved footing is unavailable because a saved amount or Difference entry is invalid.'


@router.get("/api/trial-balance/uploads")
def list_trial_balance_uploads(client_id: int):
    """
    Available Trial Balances, Section 1/Phase 1: list every upload for a
    client, so the upcoming "Available Trial Balances" page (Phase 2) has
    data to populate its dropdown. Returns just enough per upload (period
    dates/type, status, row_count) to build a label like "Q1 2026 (Jul-Sep)"
    without a second call per row.

    Ordered most-recent-period-first (period_end desc), since that's the
    most useful default when browsing - falls back to uploaded_at desc for
    any row with no period_end (e.g. an upload made before Section 2's
    period fields existed), so older/legacy rows still sort predictably
    instead of being pushed to an arbitrary position by a null comparison.

    No filtering/pagination this phase, per the locked scope - a client's
    full upload history is returned in one call.
    """
    client_rows = execute_query_pg("SELECT id FROM clients WHERE id = %s AND is_active = TRUE", (client_id,))
    if not client_rows:
        raise HTTPException(404, "Client not found")

    rows = execute_query_pg(
        """
        SELECT id, period_start, period_end, period_type, status, row_count
        FROM trial_balance_uploads
        WHERE client_id = %s
        ORDER BY (period_end IS NULL), period_end DESC, uploaded_at DESC
        """,
        (client_id,)
    )

    uploads = [
        {
            "id": r["id"],
            "period_start": r["period_start"],
            "period_end": r["period_end"],
            "period_type": r["period_type"],
            "status": r["status"],
            "row_count": r["row_count"],
        }
        for r in rows
    ]

    return {"client_id": client_id, "uploads": uploads}


@router.delete("/api/trial-balance/uploads/{upload_id}")
def delete_trial_balance_upload(upload_id: int):
    """
    Delete an upload and all its account rows. trial_balance_accounts.upload_id
    declares ON DELETE CASCADE, but this app's connections never enable
    PRAGMA foreign_keys = ON (documented elsewhere in this file/codebase), so
    the cascade would not actually fire - accounts are deleted explicitly
    first, then the upload row, same two-step order/style already used by the
    Section 2/Phase 5 overwrite-conflict delete above.

    If this delete leaves the client with zero remaining uploads, also clears
    their account_category_assignments (the persistent mirror table that
    carries categorizations forward across TB re-uploads) - confirmed fix:
    without a live TB, get_category_for_account() falls back to this table,
    which otherwise keeps returning stale categories forever. Only clears
    on zero-remaining-uploads, not on every delete, so the carry-forward
    behavior stays intact for a client who still has another upload.
    """
    _require_upload(upload_id)

    upload_rows = execute_query_pg("SELECT client_id FROM trial_balance_uploads WHERE id = %s", (upload_id,))
    client_id = upload_rows[0]["client_id"] if upload_rows else None

    # Count first so the audit log can name how many account rows went with it.
    count_rows = execute_query_pg(
        "SELECT COUNT(*) as count FROM trial_balance_accounts WHERE upload_id = %s",
        (upload_id,)
    )
    account_count = count_rows[0]["count"] if count_rows else 0

    execute_write_pg("DELETE FROM trial_balance_rollforward_adjustments WHERE upload_id = %s", (upload_id,))
    execute_write_pg("DELETE FROM trial_balance_accounts WHERE upload_id = %s", (upload_id,))
    execute_write_pg("DELETE FROM trial_balance_uploads WHERE id = %s", (upload_id,))

    assignments_cleared = False
    if client_id is not None:
        remaining = execute_query_pg(
            "SELECT COUNT(*) as count FROM trial_balance_uploads WHERE client_id = %s",
            (client_id,)
        )
        if remaining and remaining[0]["count"] == 0:
            execute_write_pg("DELETE FROM account_category_assignments WHERE client_id = %s", (client_id,))
            invalidate_category_for_account_cache(client_id)
            assignments_cleared = True

    log_audit("Analyst", "Delete", "TrialBalance",
               f"Deleted trial balance upload {upload_id} and its {account_count} account row(s)"
               + (f"; client {client_id} had no uploads left, so its category assignments were also cleared"
                  if assignments_cleared else ""))

    return {"upload_id": upload_id, "deleted": True, "deleted_account_count": account_count,
            "assignments_cleared": assignments_cleared}


@router.get("/api/trial-balance/summary")
def get_trial_balance_summary(upload_id: int):
    """
    Counts only (total/leaf/parent) for one upload - no ambiguity-detection
    logic here, since the classification engine (Phase 2) doesn't flag any
    row as ambiguous; that would be a separate, not-yet-confirmed scope
    addition, not something to assume in this phase.
    """
    _require_upload(upload_id)

    rows = execute_query_pg(
        "SELECT classification, COUNT(*) as count FROM trial_balance_accounts WHERE upload_id = %s GROUP BY classification",
        (upload_id,)
    )
    counts = {r["classification"]: r["count"] for r in rows}
    leaf_count = counts.get("leaf", 0)
    parent_count = counts.get("parent", 0)

    return {
        "upload_id": upload_id,
        "total_count": leaf_count + parent_count,
        "leaf_count": leaf_count,
        "parent_count": parent_count,
    }


# ---------- ACCOUNT CODE / CLASSIFICATION CORRECTIONS ----------
# Code corrections are scoped to one selected upload. Classification overrides remain
# row-level and preserve their explicit manual status across later code corrections.

def _save_account_code_corrections(upload_id: int, corrections: list[AccountCodeCorrection]):
    """Validate the whole draft, carry mappings and rebuild one upload atomically."""
    if not corrections or len({item.account_id for item in corrections}) != len(corrections):
        raise HTTPException(400, "Provide distinct accounts to correct.")
    with pg_transaction():
        uploads = execute_query_pg(
            "SELECT id, client_id, status FROM trial_balance_uploads WHERE id = %s FOR UPDATE", (upload_id,))
        if not uploads:
            raise HTTPException(404, "Trial Balance not found.")
        upload = uploads[0]
        if upload["status"] != "parsed":
            raise HTTPException(409, "Only a successfully parsed Trial Balance can be corrected.")
        rows = execute_query_pg(
            "SELECT id, account_code, classification, manually_overridden FROM trial_balance_accounts "
            "WHERE upload_id = %s ORDER BY id FOR UPDATE", (upload_id,))
        by_id = {row["id"]: row for row in rows}
        occupied = {row["account_code"]: row["id"] for row in rows}
        changes = {}
        for item in corrections:
            row = by_id.get(item.account_id)
            if not row:
                raise HTTPException(404, "A corrected account is not in this Trial Balance.")
            if row["account_code"] != item.expected_account_code:
                raise HTTPException(409, "Account codes changed elsewhere. Reload before correcting them.")
            code = item.account_code
            if not re.fullmatch(r"[0-9]+(?:-[0-9]+)*", code) or len(code) > 120:
                raise HTTPException(400, "Use numbers and single hyphens only, e.g. 10-10-25.")
            if code in occupied and occupied[code] != item.account_id:
                raise HTTPException(409, f"Account code {code} already belongs to another account. Choose an unused code.")
            if code != row["account_code"]:
                changes[item.account_id] = code
        final_codes = [changes.get(row["id"], row["account_code"]) for row in rows]
        if len(set(final_codes)) != len(final_codes):
            raise HTTPException(409, "Corrections would create duplicate account codes.")
        if not changes:
            return {"updated": False, "upload_id": upload_id, "corrected_count": 0}
        hierarchy = build_account_code_hierarchy(final_codes)
        for account_id, code in changes.items():
            _copy_shared_code_assignments(upload["client_id"], by_id[account_id]["account_code"], code, upload_id)
        values = []
        for row, code in zip(rows, final_codes):
            values.append((row["id"], code, hierarchy["parent_account_code"].get(code), len(code.split('-')),
                           row["classification"] if row["manually_overridden"] else hierarchy["classification"][code]))
        # Bounded set updates avoid one network round trip per account. Keep all
        # chunks in the same transaction and skip hierarchy rows that did not change.
        for offset in range(0, len(values), 1000):
            chunk = values[offset:offset + 1000]
            placeholders = ','.join(['(%s::integer,%s::text,%s::text,%s::integer,%s::text)'] * len(chunk))
            execute_write_pg(
                "UPDATE trial_balance_accounts AS a SET source_account_code = COALESCE(a.source_account_code, a.account_code), "
                "account_code = v.code, parent_account_code = v.parent, level = v.level, classification = v.classification "
                f"FROM (VALUES {placeholders}) AS v(id, code, parent, level, classification) "
                "WHERE a.id = v.id AND a.upload_id = %s AND "
                "(a.account_code, a.parent_account_code, a.level, a.classification) IS DISTINCT FROM "
                "(v.code, v.parent, v.level, v.classification)",
                tuple(value for entry in chunk for value in entry) + (upload_id,))
    invalidate_category_for_account_cache(upload["client_id"])
    log_audit("Analyst", "Update", "TrialBalance", f"Corrected {len(changes)} account code(s) in upload {upload_id}: " +
              '; '.join(f'{by_id[account_id]["account_code"]} -> {code}' for account_id, code in changes.items()))
    return {"updated": True, "upload_id": upload_id, "corrected_count": len(changes)}


@router.put("/api/trial-balance/uploads/{upload_id}/account-codes")
def update_trial_balance_account_codes(upload_id: int, payload: AccountCodeBatchRequest):
    return _save_account_code_corrections(upload_id, payload.corrections)


@router.put("/api/trial-balance/accounts/{account_id}/code")
def update_trial_balance_account_code(account_id: int, payload: AccountCodeEditRequest):
    """Retain the existing single-account API with the same batched hierarchy save."""
    rows = execute_query_pg("SELECT upload_id, account_code, source_account_code FROM trial_balance_accounts WHERE id = %s", (account_id,))
    if not rows:
        raise HTTPException(404, "Trial balance account not found")
    account = rows[0]
    result = _save_account_code_corrections(account["upload_id"], [AccountCodeCorrection(
        account_id=account_id, account_code=payload.account_code, expected_account_code=account["account_code"])])
    return {**result, "account_id": account_id, "account_code": payload.account_code.strip(),
            "source_account_code": account.get("source_account_code") or account["account_code"]}


@router.put("/api/trial-balance/accounts/{account_id}/override")
def override_account_classification(account_id: int, payload: AccountOverrideRequest):
    """
    Manually flip one account's classification (leaf <-> parent) and mark it as
    manually_overridden. Returns the updated row in the same shape
    list_trial_balance_accounts() already uses, so the frontend can splice the
    response straight into its rendered row with no reshaping.
    """
    account_rows = execute_query_pg("SELECT id FROM trial_balance_accounts WHERE id = %s", (account_id,))
    if not account_rows:
        raise HTTPException(404, "Trial balance account not found")

    new_classification = 'leaf' if payload.is_leaf else 'parent'
    execute_write_pg(
        "UPDATE trial_balance_accounts SET classification = %s, manually_overridden = TRUE WHERE id = %s",
        (new_classification, account_id)
    )

    updated_rows = execute_query_pg(
        "SELECT id, account_code, account_name, level, classification, manually_overridden, upload_id "
        "FROM trial_balance_accounts WHERE id = %s",
        (account_id,)
    )
    r = updated_rows[0]

    log_audit("Analyst", "Update", "TrialBalance",
               f"Manually reclassified account {r['account_code']} as {new_classification} "
               f"(upload_id={r['upload_id']})")

    return {
        "id": r["id"],
        "account_code": r["account_code"],
        "account_name": r["account_name"],
        "level": r["level"],
        "is_leaf": r["classification"] == "leaf",
        "manually_overridden": bool(r["manually_overridden"]),
    }


# ---------- ASSIGN CATEGORIES (Section 2, Phase 4: latest-TB lookup + cascade assignment) ----------
# Lets an auditor tag trial_balance_accounts rows with an Audit Rules Category
# (audit_rule_categories, see routers/audit_rules.py). category_id already
# exists on trial_balance_accounts (Phase 3 schema) - this phase is backend
# logic only, no new schema.

def _validate_active_category(category_id: int) -> None:
    """Category must exist and be active before any write - mirrors this
    file's existing _require_upload() style. audit_rule_categories has no
    dedicated getter helper in database.py, so this queries it directly."""
    rows = execute_query_pg(
        "SELECT id FROM audit_rule_categories WHERE id = %s AND is_active = TRUE",
        (category_id,)
    )
    if not rows:
        raise HTTPException(400, f"Category {category_id} not found or inactive")


def upsert_account_category_assignment(client_id: int, account_code: str, category_id: Optional[int]) -> None:
    """
    Verification <-> Assign Categories link, Phase 2: single write point into
    the persistent account_category_assignments table (Phase 1 schema,
    database.py). Keyed by client_id + account_code (UNIQUE constraint), so
    this is always an upsert - insert on first assignment, overwrite on every
    later change. category_id may be None (clearing a category), which is
    persisted as-is - the persistent table mirrors trial_balance_accounts'
    own category_id exactly, including "explicitly cleared".
    """
    execute_write_pg(
        """
        INSERT INTO account_category_assignments (client_id, account_code, category_id)
        VALUES (%s, %s, %s)
        ON CONFLICT(client_id, account_code)
        DO UPDATE SET category_id = excluded.category_id, updated_at = CURRENT_TIMESTAMP
        """,
        (client_id, account_code, category_id)
    )


def find_latest_parsed_upload(client_id: int):
    """
    Shared "latest Trial Balance" lookup - the most recent upload (by
    period_end, falling back to uploaded_at as tiebreaker for uploads made
    before period tracking existed) with status='parsed'. Used by both
    get_latest_trial_balance_accounts() and reset_trial_balance_categories()
    (confirmed fix) so "latest" is defined identically in both places.
    Returns the raw upload row, or None if no parsed upload exists.
    """
    upload_rows = execute_query_pg(
        """
        SELECT id, period_start, period_end, period_type, as_of_date
        FROM trial_balance_uploads
        WHERE client_id = %s AND status = 'parsed'
        ORDER BY (period_end IS NULL), period_end DESC, uploaded_at DESC
        LIMIT 1
        """,
        (client_id,)
    )
    return upload_rows[0] if upload_rows else None


@router.get("/api/trial-balance/latest-accounts")
def get_latest_trial_balance_accounts(client_id: int):
    """
    Latest Trial Balance for a client - the most recent upload (by
    period_end, falling back to uploaded_at as tiebreaker for uploads made
    before period tracking existed) with status='parsed'. Returns every
    account with its current category assignment (if any), for the Assign
    Categories page (Phase 5).
    """
    client_rows = execute_query_pg("SELECT id FROM clients WHERE id = %s AND is_active = TRUE", (client_id,))
    if not client_rows:
        raise HTTPException(404, "Client not found")

    upload = find_latest_parsed_upload(client_id)
    if not upload:
        raise HTTPException(404, "No parsed trial balance found for this client")

    rows = execute_query_pg(
        """
        SELECT a.id, a.account_code, a.account_name, a.classification, a.category_id,
               c.name AS category_name
        FROM trial_balance_accounts a
        LEFT JOIN audit_rule_categories c ON a.category_id = c.id
        WHERE a.upload_id = %s
        ORDER BY a.sort_order
        """,
        (upload["id"],)
    )

    accounts = [
        {
            "id": r["id"],
            "account_code": r["account_code"],
            "account_name": r["account_name"],
            "is_leaf": r["classification"] == "leaf",
            "category_id": r["category_id"],
            "category_name": r["category_name"],
        }
        for r in rows
    ]

    return {
        "period_start": upload["period_start"],
        "period_end": upload["period_end"],
        "period_type": upload["period_type"],
        "as_of_date": upload["as_of_date"],
        "accounts": accounts,
    }


@router.put("/api/trial-balance/assign-category")
def assign_trial_balance_category(payload: CategoryAssignmentRequest):
    """
    Assign an Audit Rules Category to one trial balance account, with
    parent-to-child cascade. The target account's own category_id is always
    set. If the target is a leaf, that's the whole operation (force_apply is
    a no-op for a leaf - there's nothing to cascade into).

    If the target is a parent, every descendant (recursively, via the
    parent_account_code hierarchy already persisted by the classification
    engine - no new hierarchy logic here) is cascaded into:
      - smart-fill (force_apply=False, default): only descendants whose
        category_id is currently NULL are updated: any descendant that
        already carries a category (user-set or previously cascaded) is
        left alone.
      - force-apply (force_apply=True): every descendant is overwritten
        regardless of its current value. Only reached via an explicit UI
        action (Phase 5), never the default path.
    """
    account_rows = execute_query_pg("SELECT * FROM trial_balance_accounts WHERE id = %s", (payload.account_id,))
    if not account_rows:
        raise HTTPException(404, "Trial balance account not found")
    target = account_rows[0]

    hierarchy_rows = execute_query_pg(
        "SELECT id, account_code, classification, manually_overridden FROM trial_balance_accounts WHERE upload_id = %s",
        (target["upload_id"],),
    )
    hierarchy = build_account_code_hierarchy(row["account_code"] for row in hierarchy_rows)
    effective_classification = {
        row["id"]: row["classification"] if row["manually_overridden"] else hierarchy['classification'][row["account_code"]]
        for row in hierarchy_rows
    }
    parent_account_codes = hierarchy['parent_account_code']

    # category_id is nullable (confirmed fix) - null means "Not assigned", so
    # the active-category check only applies when a real category is given.
    if payload.category_id is not None:
        _validate_active_category(payload.category_id)

    # client_id isn't a column on trial_balance_accounts (reached via
    # upload_id -> trial_balance_uploads.client_id) - needed for the
    # persistent account_category_assignments upserts below (Verification
    # link, Phase 2), keyed by client_id + account_code.
    upload_rows = execute_query_pg(
        "SELECT client_id FROM trial_balance_uploads WHERE id = %s", (target["upload_id"],)
    )
    client_id = upload_rows[0]["client_id"] if upload_rows else None

    # Target account's own assignment is unconditional - this is the explicit
    # action requested, regardless of force_apply or its current value. When
    # payload.category_id is None this writes NULL, i.e. clears the category.
    execute_write_pg(
        "UPDATE trial_balance_accounts SET category_id = %s WHERE id = %s",
        (payload.category_id, payload.account_id)
    )
    if client_id is not None:
        upsert_account_category_assignment(client_id, target["account_code"], payload.category_id)

    is_leaf = effective_classification.get(target["id"], target["classification"]) == "leaf"
    descendants_updated = []
    descendants_skipped = []

    if not is_leaf:
        # Fetch every account in this upload once, build a parent_code ->
        # [child rows] map, then walk down from the target's own code to
        # collect every descendant (parents and leaves alike), not just
        # direct children.
        # LEFT JOIN in the category's own is_active state (inactive-selection
        # requirement, Phase 4) so smart-fill below can tell a descendant's
        # existing assignment apart from a stale one pointing at a since-
        # deactivated Category - the two are treated identically ("empty").
        sibling_rows = execute_query_pg(
            """
            SELECT s.id, s.account_code, s.parent_account_code, s.category_id,
                   s.classification, s.manually_overridden,
                   c.is_active AS category_is_active
            FROM trial_balance_accounts s
            LEFT JOIN audit_rule_categories c ON c.id = s.category_id
            WHERE s.upload_id = %s
            """,
            (target["upload_id"],)
        )
        children_by_parent_code = {}
        for row in sibling_rows:
            parent_code = parent_account_codes.get(row["account_code"])
            if parent_code:
                children_by_parent_code.setdefault(parent_code, []).append(row)

        descendants = []
        stack = list(children_by_parent_code.get(target["account_code"], []))
        while stack:
            row = stack.pop()
            descendants.append(row)
            stack.extend(children_by_parent_code.get(row["account_code"], []))

        for row in descendants:
            # A descendant with no category, or whose current category has
            # since been deactivated, counts as "empty" for smart-fill - a
            # stale assignment pointing at an inactive Category is no more
            # a real categorization than no assignment at all (confirmed).
            currently_empty = row["category_id"] is None or not row["category_is_active"]
            row_is_leaf = effective_classification.get(row["id"], row["classification"]) == "leaf"
            if row_is_leaf and (payload.force_apply or currently_empty):
                descendants_updated.append(row["id"])
            elif row_is_leaf:
                descendants_skipped.append(row["id"])

        if descendants_updated:
            placeholders = ",".join("%s" for _ in descendants_updated)
            execute_write_pg(
                f"UPDATE trial_balance_accounts SET category_id = %s WHERE id IN ({placeholders})",
                tuple([payload.category_id] + descendants_updated)
            )
            # Mirror every actually-updated descendant into the persistent
            # table too (Verification link, Phase 2) - descendants_skipped
            # (smart-fill leaving an already-categorized row alone) are
            # deliberately NOT touched here, since their category_id on
            # trial_balance_accounts itself didn't change.
            if client_id is not None:
                updated_id_set = set(descendants_updated)
                for row in descendants:
                    if row["id"] in updated_id_set:
                        upsert_account_category_assignment(client_id, row["account_code"], payload.category_id)

    if client_id is not None:
        invalidate_category_for_account_cache(client_id)

    category_label = payload.category_id if payload.category_id is not None else "None (cleared)"
    log_audit("Analyst", "Update", "TrialBalance",
               f"Assigned category {category_label} to account {target['account_code']} "
               f"(id={payload.account_id}, force_apply={payload.force_apply}); "
               f"{len(descendants_updated)} descendant(s) updated, {len(descendants_skipped)} skipped")

    return {
        "account_id": payload.account_id,
        "category_id": payload.category_id,
        "force_apply": payload.force_apply,
        "is_leaf": is_leaf,
        "descendants_updated_count": len(descendants_updated),
        "descendants_skipped_count": len(descendants_skipped),
        "descendants_updated": descendants_updated,
        "descendants_skipped": descendants_skipped,
    }


@router.post("/api/trial-balance/reset-categories")
def reset_trial_balance_categories(client_id: int):
    """
    Confirmed fix: bulk "Reset" for the Assign Categories page. Sets every
    account's category_id to NULL for the client's CURRENTLY LOADED (latest
    parsed) Trial Balance only - not historical uploads - using the same
    "latest" lookup as get_latest_trial_balance_accounts() (via
    find_latest_parsed_upload()), so the two endpoints can never disagree on
    which upload "the current one" means.
    """
    client_rows = execute_query_pg("SELECT id FROM clients WHERE id = %s AND is_active = TRUE", (client_id,))
    if not client_rows:
        raise HTTPException(404, "Client not found")

    upload = find_latest_parsed_upload(client_id)
    if not upload:
        raise HTTPException(404, "No parsed trial balance found for this client")
    upload_id = upload["id"]

    # Count first so the audit log/response can say how many rows actually
    # had a category cleared (rather than reporting the upload's full row
    # count, which would overstate a reset that finds most rows already
    # unassigned).
    count_rows = execute_query_pg(
        "SELECT COUNT(*) as count FROM trial_balance_accounts WHERE upload_id = %s AND category_id IS NOT NULL",
        (upload_id,)
    )
    reset_count = count_rows[0]["count"] if count_rows else 0

    execute_write_pg("UPDATE trial_balance_accounts SET category_id = NULL WHERE upload_id = %s", (upload_id,))

    log_audit("Analyst", "Update", "TrialBalance",
               f"Reset all category assignments for client {client_id}'s latest trial balance "
               f"(upload_id={upload_id}); {reset_count} account(s) had a category cleared")

    return {"upload_id": upload_id, "reset_count": reset_count}
