"""Exact, compact upload controls; transaction occurrences are never grouped away."""

from datetime import date, datetime
from zoneinfo import ZoneInfo
from decimal import Decimal, InvalidOperation, localcontext
import math
import re

from database import resolve_document_column


TOTAL_LABEL = re.compile(r'(?:grand\s*)?(?:sub\s*)?total\s*:?$', re.I)
NUMBER_PATTERN = r'[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?'
NUMBER = re.compile(NUMBER_PATTERN)
# Compare at full Decimal precision. These are decision thresholds only; no
# source or transaction amount is rounded or rewritten.
FOOTING_AUTO_ACCEPT_BELOW = Decimal('0.01')
FOOTING_CONFIRMATION_LIMIT = Decimal('1.00')
# Match Python str.strip() for identifiers and numeric text, including ERP NBSP.
SOURCE_WHITESPACE = ' \t\n\r\v\f\x1c\x1d\x1e\x1f\x85\xa0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a\u2028\u2029\u202f\u205f\u3000'


def footing_columns(columns):
    return [column for column in columns if column.get('reconciliation_role') == 'footing']


def blank(value):
    # pandas represents genuinely empty Excel cells as NaN. The workbook input
    # boundary independently rejects literal nonfinite/error cells.
    return value is None or (isinstance(value, float) and math.isnan(value)) or str(value).strip() == ''


def amount(value, *, required=False):
    # Native Excel numbers need no text-format regex or thousand-digit tuple.
    # Keep this common path small: each detail amount participates in two sums.
    if type(value) is int:
        result = Decimal(value)
        if result.adjusted() > 1000:
            raise ValueError('Enter a finite amount within the supported numeric range.')
        return result
    if type(value) is float and math.isfinite(value):
        return Decimal(str(value))
    if blank(value):
        if required:
            raise ValueError('A numeric total is required. Recalculate and save Excel formulas before uploading.')
        return Decimal(0)
    text = str(value).strip()
    if isinstance(value, bool) or not NUMBER.fullmatch(text):
        raise ValueError('Enter a valid numeric amount in Excel.')
    try:
        result = Decimal(text)
    except InvalidOperation as error:
        raise ValueError('Enter a valid numeric amount in Excel.') from error
    if not result.is_finite() or abs(result.adjusted()) > 1000 or len(result.as_tuple().digits) > 1000:
        raise ValueError('Enter a finite amount within the supported numeric range.')
    return result


def selected_control_row(records, columns, mapping, row_number, confirmed):
    """Validate the selected control row only through configured Footing cells."""
    footings = footing_columns(columns)
    if not footings:
        if row_number is not None or confirmed:
            raise ValueError('Template Footing settings changed. Review the mapping and validate again.')
        return None
    key = resolve_document_column(columns)['column_name']
    for column in [key] + [c['column_name'] for c in footings]:
        if not mapping.get(column) or any(mapping[column] not in row for row in records):
            raise ValueError(f'Map the reconciliation field "{column}" to an Excel column.')
    if not confirmed or type(row_number) is not int or not 2 <= row_number <= len(records) + 1:
        raise ValueError('Confirm a valid Excel control-total row number (header is row 1).')
    raw = records[row_number - 2]
    for column in footings:
        name = column['column_name']
        if column['data_type'] != 'Numeric':
            raise ValueError(f'Template Footing field "{name}" must be Numeric.')
        try:
            amount(raw.get(mapping[name]), required=True)
        except ValueError as error:
            raise ValueError(f'Excel row {row_number}, {name}: {error}') from error
    return row_number


def summarize_rows(rows, columns, mapping, row_number=lambda row, fallback: fallback):
    """SUM/COUNT/DISTINCT over source occurrences, with decimal strings on the wire."""
    key = resolve_document_column(columns)['column_name']
    document_column = mapping.get(key)
    names = [c['column_name'] for c in footing_columns(columns)]
    totals = {name: Decimal(0) for name in names}
    documents, missing, count = set(), 0, 0
    with localcontext() as context:
        context.prec = 2010 + len(str(len(rows)))
        for fallback, row in enumerate(rows, 2):
            count += 1
            value = row.get(document_column)
            if blank(value):
                missing += 1
            else:
                documents.add(str(value).strip())
            for name in names:
                try:
                    totals[name] += amount(row.get(mapping.get(name)))
                except ValueError as error:
                    raise ValueError(f'Excel row {row_number(row, fallback)}, {name}: {error}') from error
    return {'row_count': count, 'document_count': len(documents), 'missing_document_rows': missing,
            'footing_totals': {name: format(value, 'f') for name, value in totals.items()}}


def reconcile_source(footer_values, detail_rows, columns, mapping, row_number, row_number_for,
                     confirm_variance=False):
    """A report footer proves the full detail, never a user-selected subset."""
    summary = summarize_rows(detail_rows, columns, mapping, row_number_for)
    checks = []
    confirmable_variance = False
    with localcontext() as context:
        context.prec = 2010 + len(str(len(detail_rows)))
        for column in footing_columns(columns):
            name = column['column_name']
            reported = amount(footer_values.get(mapping[name]), required=True)
            actual = Decimal(summary['footing_totals'][name])
            difference = actual - reported
            magnitude = abs(difference)
            if difference == 0:
                status = 'matched'
            elif magnitude < FOOTING_AUTO_ACCEPT_BELOW:
                status = 'rounding_tolerated'
            elif magnitude <= FOOTING_CONFIRMATION_LIMIT:
                confirmable_variance = True
                status = 'confirmed_variance' if confirm_variance else 'confirmation_required'
            else:
                status = 'mismatch'
            checks.append({'column': name, 'excel_total': format(reported, 'f'),
                           'detail_total': format(actual, 'f'), 'difference': format(difference, 'f'),
                           'status': status})
    if confirm_variance and not confirmable_variance:
        raise ValueError('No Footing variance is within the confirmation range. Validate again without variance confirmation.')
    if not checks:
        status = 'not_configured'
    elif any(check['status'] == 'mismatch' for check in checks):
        status = 'mismatch'
    elif any(check['status'] == 'confirmation_required' for check in checks):
        status = 'confirmation_required'
    elif any(check['status'] == 'confirmed_variance' for check in checks):
        status = 'confirmed_variance'
    elif any(check['status'] == 'rounding_tolerated' for check in checks):
        status = 'rounding_tolerated'
    else:
        status = 'matched'
    return {'status': status, 'control_total_row': row_number, 'checks': checks, 'full_sheet': summary,
            'tolerance_policy': {'auto_accept_below': format(FOOTING_AUTO_ACCEPT_BELOW, 'f'),
                                 'confirmation_limit': format(FOOTING_CONFIRMATION_LIMIT, 'f'),
                                 'variance_confirmed': bool(confirm_variance)}}


def aggregate_stored_controls(query, columns, client_id, template_id, start, end, *, ledger_account_code=None):
    """One scoped PostgreSQL aggregate, ready for post-import assurance.

    Call with the existing transaction/snapshot query helper. Invalid stored
    amounts fail visibly; this helper neither caches totals nor fetches rows.
    All JSON keys and scope values are parameters, never SQL identifiers.
    """
    if date.fromisoformat(start) > date.fromisoformat(end):
        raise ValueError('Start date must be on or before the end date.')
    where = """cd.client_id = %s AND cd.template_id = %s AND cd.is_deleted = FALSE
        AND cd.change_detection_status != 'deleted' AND cd.extract_date BETWEEN %s::date AND %s::date"""
    params = [client_id, template_id, start, end]
    if ledger_account_code is not None:
        where += ' AND EXISTS (SELECT 1 FROM uploaded_files u WHERE u.id = cd.upload_id AND u.ledger_account_code = %s)'
        params.append(ledger_account_code)
    return aggregate_filtered_controls(query, columns, where, params)


def aggregate_filtered_controls(query, columns, where_sql, where_params):
    """Reuse exact controls with a trusted backend-built, parameterized filter.

    Preview counts and financial sums must share the page's complete WHERE
    clause; LIMIT/OFFSET and rendered subtotal rows never participate.
    """
    if not columns:
        raise ValueError('Template columns are unavailable for summary controls.')
    key = resolve_document_column(columns)['column_name']
    names = [c['column_name'] for c in footing_columns(columns)]
    projection = ["NULLIF(BTRIM(cd.row_data ->> %s, %s), '') AS document_id"]
    params = [key, SOURCE_WHITESPACE]
    for index, name in enumerate(names):
        projection.append(f"NULLIF(BTRIM(cd.row_data ->> %s, %s), '') AS amount_{index}")
        params.extend([name, SOURCE_WHITESPACE])
    params.extend(where_params)
    aggregates = ['COUNT(*) AS row_count', 'COUNT(DISTINCT document_id) AS document_count',
                  'COUNT(*) FILTER (WHERE document_id IS NULL) AS missing_document_rows']
    for index in range(len(names)):
        aggregates.extend([
            f'COALESCE(SUM(CASE WHEN amount_{index} ~ %s THEN amount_{index}::numeric ELSE 0 END), 0) AS total_{index}',
            f'COUNT(*) FILTER (WHERE amount_{index} IS NOT NULL AND amount_{index} !~ %s) AS invalid_{index}'])
        params.extend(['^' + NUMBER_PATTERN + '$'] * 2)
    result = query(f"""WITH source AS (SELECT {', '.join(projection)} FROM client_data cd
        WHERE {where_sql}) SELECT {', '.join(aggregates)} FROM source""", tuple(params))[0]
    if any(result[f'invalid_{index}'] for index in range(len(names))):
        raise ValueError('Stored Footing values contain invalid amounts; reconciliation is unavailable.')
    return {'row_count': int(result['row_count']), 'document_count': int(result['document_count']),
            'missing_document_rows': int(result['missing_document_rows']),
            'footing_totals': {name: format(Decimal(str(result[f'total_{index}'])), 'f') for index, name in enumerate(names)}}


def stored_reconciliation(source, columns, scope, stored=None, *, unavailable=False):
    """Build a bounded receipt from independent approved-detail and SQL metrics."""
    expected = (source or {}).get('approved_detail')
    result = {'scope': scope, 'checked_at': datetime.now(ZoneInfo('Asia/Karachi')).isoformat(),
              'document_column': resolve_document_column(columns)['column_name'],
              'footing_configured': bool(footing_columns(columns)), 'rows': [],
              'status': 'unavailable', 'message': 'Stored-data verification is unavailable. No reconciliation is certified.'}
    metrics = [('row_count', 'Total rows'), ('document_count', 'Unique documents')]
    for key, label in metrics:
        result['rows'].append({'key': key, 'label': label, 'excel': (expected or {}).get(key),
                               'stored': (stored or {}).get(key)})
    for column in footing_columns(columns):
        name = column['column_name']
        result['rows'].append({'key': 'footing', 'label': f'Footing — {name}',
            'excel': (expected or {}).get('footing_totals', {}).get(name),
            'stored': (stored or {}).get('footing_totals', {}).get(name)})
    comparable = bool(expected and stored and scope.get('complete_period') and not unavailable)
    with localcontext() as context:
        context.prec = 2020
        for row in result['rows']:
            row['difference'] = (format(Decimal(str(row['stored'])) - Decimal(str(row['excel'])), 'f')
                if comparable and row['excel'] is not None and row['stored'] is not None else None)
            row['matches'] = Decimal(row['difference']) == 0 if row['difference'] is not None else None
    if expected and not scope.get('complete_period') and not unavailable:
        result.update(status='not_comparable', message='Partial extract: Excel detail and the complete stored period have different scopes. Full-period reconciliation is not certified.')
        return result
    if unavailable or not expected or stored is None:
        return result
    if any(row['matches'] is not True for row in result['rows']):
        result.update(status='mismatch', message='Reconciliation differences need review. Stored data does not match the approved Excel controls.')
    elif expected.get('missing_document_rows') or stored.get('missing_document_rows'):
        result.update(status='incomplete', message='Some detail rows have no Document ID. Document completeness is not certified.')
    else:
        result.update(status='matched', message='Approved Excel controls match stored data at completion.' if result['footing_configured']
                      else 'Row and document counts match stored data. Footing is not configured.')
    return result


def verify_stored_controls(query, source, columns, scope):
    """The caller supplies its current locked import transaction/read boundary."""
    period = scope.get('date_range') or {}
    if not (source or {}).get('approved_detail') or not period.get('start') or not period.get('end'):
        return stored_reconciliation(source, columns, scope,
            unavailable=bool(scope.get('complete_period') or not (source or {}).get('approved_detail')))
    stored = aggregate_stored_controls(query, columns, scope['client_id'], scope['template_id'],
                                      period['start'], period['end'], ledger_account_code=scope.get('ledger_account_code'))
    return stored_reconciliation(source, columns, scope, stored)
