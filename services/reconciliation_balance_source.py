"""Normalize stored opening and month-end balances for reconciliation proofs."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from bisect import bisect_left
from collections import Counter, defaultdict
import hashlib
import json
import math
from typing import Any, Callable, Mapping, Sequence

from database import execute_query_pg
from services.ledger_group_summary import build_cashflow_presentation_adjustments


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _code(value: Any) -> str:
    return _text(value)


def normalize_balance_month(value: Any) -> str:
    """Return a canonical ``YYYY-MM`` month key without changing its period."""

    if isinstance(value, datetime):
        return value.strftime("%Y-%m")
    if isinstance(value, date):
        return value.strftime("%Y-%m")
    raw = _text(value)
    if len(raw) == 7 and raw[4] == "-":
        year, month = raw[:4], raw[5:]
        if year.isdigit() and month.isdigit() and 1 <= int(month) <= 12:
            return raw
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        raise ValueError("month must be in YYYY-MM or ISO date format") from None
    return parsed.strftime("%Y-%m")


def _period_months(period_start: Any, period_end: Any) -> list[str]:
    start = normalize_balance_month(period_start)
    end = normalize_balance_month(period_end)
    start_year, start_month = int(start[:4]), int(start[5:])
    end_year, end_month = int(end[:4]), int(end[5:])
    if (start_year, start_month) > (end_year, end_month):
        raise ValueError("period_start and period_end must form a valid period")
    months: list[str] = []
    year, month = start_year, start_month
    while (year, month) <= (end_year, end_month):
        months.append(f"{year:04d}-{month:02d}")
        month += 1
        if month == 13:
            year += 1
            month = 1
    return months


def _numeric(value: Any, field: str) -> float:
    if value is None or (isinstance(value, str) and not value.strip()):
        raise ValueError(f"{field} is required")
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{field} must be numeric") from None
    if not math.isfinite(number):
        raise ValueError(f"{field} must be finite")
    return number


@dataclass(frozen=True)
class ReconciliationBalanceSource:
    """Canonical balance inputs plus non-silent completeness diagnostics."""

    opening_balances: Mapping[str, float]
    actual_closing_balances: Mapping[str, Mapping[str, float]]
    diagnostics: tuple[dict[str, Any], ...]
    months: tuple[str, ...]

    @property
    def ready(self) -> bool:
        return not any(item.get("severity") == "error" for item in self.diagnostics)

    def blocking_diagnostics(self) -> list[dict[str, Any]]:
        return [item for item in self.diagnostics if item.get("severity") == "error"]


def build_balance_source(
    rows: Sequence[Mapping[str, Any]] | None,
    group_account_codes: Sequence[str],
    period_start: Any,
    period_end: Any,
) -> ReconciliationBalanceSource:
    """Validate balance rows and return the maps consumed by the summary engine."""

    diagnostics: list[dict[str, Any]] = []
    try:
        months = _period_months(period_start, period_end)
    except ValueError as error:
        return ReconciliationBalanceSource({}, {}, ({"code": "invalid_reporting_period", "severity": "error", "message": str(error)},), ())

    expected_codes = list(dict.fromkeys(_code(code) for code in group_account_codes if _code(code)))
    expected_set = set(expected_codes)
    opening: dict[str, float] = {}
    actual: dict[str, dict[str, float]] = {}
    seen: set[tuple[str, str]] = set()
    raw_rows = list(rows or [])
    if not raw_rows:
        diagnostics.append({"code": "no_balance_inputs", "severity": "error", "message": "No reconciliation balance inputs were provided."})

    for position, row in enumerate(raw_rows, 1):
        account_code = _code(row.get("account_code"))
        if not account_code:
            diagnostics.append({"code": "blank_balance_account_code", "severity": "error", "message": f"Balance row {position} has no account code.", "row": position})
            continue
        if account_code not in expected_set:
            diagnostics.append({"code": "balance_account_not_in_group", "severity": "error", "message": f"Account code '{account_code}' is not in the selected account group.", "account_code": account_code})
        try:
            month = normalize_balance_month(row.get("period_month", row.get("month")))
        except ValueError as error:
            diagnostics.append({"code": "invalid_balance_month", "severity": "error", "message": f"Balance row {position}: {error}", "row": position})
            continue
        key = (account_code, month)
        if key in seen:
            diagnostics.append({"code": "duplicate_balance_month", "severity": "error", "message": f"Duplicate balance input for {account_code} in {month}.", "account_code": account_code, "month": month})
            continue
        seen.add(key)
        if month not in months:
            diagnostics.append({"code": "balance_month_outside_period", "severity": "warning", "message": f"Balance input for {account_code} in {month} is outside the requested period.", "account_code": account_code, "month": month})
            continue
        try:
            opening_value = _numeric(row.get("opening_balance"), "opening_balance")
            closing_value = _numeric(row.get("actual_closing_balance"), "actual_closing_balance")
        except ValueError as error:
            diagnostics.append({"code": "invalid_balance_amount", "severity": "error", "message": f"Balance row {position}: {error}", "row": position})
            continue
        if account_code in opening and opening[account_code] != opening_value:
            diagnostics.append({"code": "inconsistent_opening_balance", "severity": "error", "message": f"Account {account_code} has more than one opening balance.", "account_code": account_code})
        opening[account_code] = opening_value
        actual.setdefault(account_code, {})[month] = closing_value

    for account_code in expected_codes:
        if account_code not in opening:
            diagnostics.append({"code": "missing_opening_balance", "severity": "error", "message": f"Opening balance is missing for account {account_code}.", "account_code": account_code})
        missing_months = [month for month in months if month not in actual.get(account_code, {})]
        if missing_months:
            diagnostics.append({"code": "missing_closing_balance", "severity": "error", "message": f"Actual closing balance is missing for account {account_code} in {missing_months}.", "account_code": account_code, "months": missing_months})

    return ReconciliationBalanceSource(opening, actual, tuple(diagnostics), tuple(months))


def load_reconciliation_balance_source(
    client_id: int,
    group_label: str,
    group_account_codes: Sequence[str],
    period_start: Any,
    period_end: Any,
    *,
    query_fn: Callable[..., list[dict[str, Any]]] = execute_query_pg,
) -> ReconciliationBalanceSource:
    """Read one saved client/group balance set and normalize it for the engine."""

    rows = query_fn(
        """
        SELECT account_code, period_month, opening_balance, actual_closing_balance
        FROM report_balance_inputs
        WHERE client_id = %s AND LOWER(group_label) = LOWER(%s)
        ORDER BY account_code, period_month
        """,
        (client_id, group_label),
    )
    return build_balance_source(rows, group_account_codes, period_start, period_end)


def _ledger_json(value: Any) -> Any:
    return json.loads(value) if isinstance(value, str) else value


def _ledger_number(value: Any) -> Decimal:
    """Read already validated numeric evidence, never reinterpret raw DR/CR text."""
    try:
        number = Decimal(str(value).replace(',', ''))
        if not number.is_finite() or not math.isfinite(float(number)):
            raise ValueError('Non-finite ledger amount')
        return number
    except (InvalidOperation, TypeError, OverflowError):
        raise ValueError('Invalid numeric ledger evidence') from None


def build_uploaded_ledger_balances(
    uploads: Sequence[Mapping[str, Any]],
    rows: Sequence[Mapping[str, Any]],
    account_types: Mapping[str, str],
    periods: Sequence[Mapping[str, Any]],
    cashflow_movements: Mapping[str, Any],
    *,
    account_names: Mapping[str, str] | None = None,
    treasury_movements: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Resolve covered boundaries from current independent ERP anchors.

    Canonical rows are counted once, including repeated occurrences retained on
    older uploads. A stale carried opening is never an anchor: reconstruct from
    a current independent anchor in the same continuous coverage component.
    No source rows or recomputed proofs are written back to the database.
    """
    diagnostics: list[dict[str, Any]] = []
    account_results: dict[str, list[dict[str, Any]]] = {}
    normalized_periods = [dict(p, start=date.fromisoformat(str(p['start'])),
                               end=date.fromisoformat(str(p['end']))) for p in periods]
    if any(p['start'] > p['end'] for p in normalized_periods):
        raise ValueError('Invalid ledger reporting period')
    if any(p['end'] == date.max for p in normalized_periods):
        raise ValueError('Ledger reporting end is outside the supported date range')

    def issue(code: str, account: str, message: str, **details: Any) -> None:
        diagnostics.append(dict(code=code, account_code=account, message=message, **details))

    for code, treasury_type in account_types.items():
        metadata = [dict(u) for u in uploads if u.get('ledger_account_code') == code]
        metadata.sort(key=lambda u: int(u['id']))
        name = (metadata[-1].get('ledger_account_name') or '') if metadata else ''
        if not name or name == code:
            name = (account_names or {}).get(code) or code
        account_rows = [r for r in rows if r.get('account_code') == code]
        timeline: list[tuple[date, int, Decimal, Mapping[str, Any]]] = []
        components: list[list[date]] = []
        anchors: list[dict[str, Any]] = []
        disputed: list[tuple[date, date]] = []
        failed = False
        if not metadata:
            issue('ledger_not_uploaded', code, 'No imported account ledger is available for this Cash/Bank account.')
        try:
            templates = {u['template_id'] for u in metadata}
            if len(templates) > 1:
                issue('ambiguous_ledger_templates', code, 'This account has several ledger templates; its source is ambiguous.')
                failed = True
            for row in account_rows:
                day = date.fromisoformat(str(row['extract_date'])[:10])
                debit, credit = (_ledger_number(row.get(side) or 0) for side in ('debit', 'credit'))
                if debit < 0 or credit < 0 or (row.get('debit') is None and row.get('credit') is None):
                    raise ValueError('Invalid canonical ledger debit/credit')
                timeline.append((day, int(row['id']), debit - credit, row))
            timeline.sort(key=lambda item: (item[0], item[1]))
            days = [item[0] for item in timeline]
            cumulative = [Decimal(0)]
            for item in timeline:
                cumulative.append(cumulative[-1] + item[2])

            def movement(start: date, end: date) -> Decimal:
                # Exclusive end makes opening-before-first-day precise.
                return cumulative[bisect_left(days, end)] - cumulative[bisect_left(days, start)]

            coverage: list[tuple[date, date]] = []
            for upload in metadata:
                evidence = _ledger_json(upload.get('ledger_evidence') or {})
                check = evidence.get('balance_check') or {}
                start = date.fromisoformat(str(upload['ledger_period_start'])[:10])
                end = date.fromisoformat(str(upload['ledger_period_end'])[:10])
                if start > end:
                    raise ValueError('Invalid declared ledger coverage')
                if not upload.get('configuration_current'):
                    issue('ledger_configuration_changed', code, 'Ledger column configuration changed; revalidate the source.', upload_id=upload['id'])
                    continue
                if check.get('source_mode') != 'replace_range' or check.get('status') in {'partial_or_incomplete', 'unordered'}:
                    continue
                coverage.append((start, end))
                # A newer complete declaration supersedes older evidence for
                # the same whole scope, even when unchanged rows kept old IDs.
                superseded = any(
                    int(new['id']) > int(upload['id']) and new.get('configuration_current')
                    and str(new['ledger_period_start'])[:10] <= start.isoformat()
                    and str(new['ledger_period_end'])[:10] >= end.isoformat()
                    and (_ledger_json(new.get('ledger_evidence') or {}).get('balance_check') or {}).get('source_mode') == 'replace_range'
                    for new in metadata)
                if superseded:
                    continue
                inside = sorted((item[3] for item in timeline[bisect_left(days, start):bisect_left(days, end + timedelta(days=1))]),
                                key=lambda r: int(r['id']))
                fingerprint = hashlib.md5(','.join(
                    f"{r['id']}:{r['version'] if r.get('version') is not None else 1}:{r.get('source_fingerprint') or ''}" for r in inside
                ).encode()).hexdigest()
                proof_current = (all(r.get('source_fingerprint') is not None for r in inside)
                                 and fingerprint == check.get('canonical_fingerprint'))
                if not proof_current:
                    issue('ledger_proof_recomputed', code, 'Stored ledger proof changed; boundaries require another current independent anchor.', upload_id=upload['id'])
                    continue
                if check.get('status') == 'differences':
                    issue('ledger_source_differences', code, 'The source ledger has balance or continuity differences; review its upload evidence.', upload_id=upload['id'])
                    disputed.append((start, end))
                    continue
                if check.get('status') != 'consistent':
                    continue  # Missing historical anchors can be reconstructed from another current root.
                if check.get('opening_source') not in {'erp_opening', 'inferred_from_transaction', 'inferred_from_closing'}:
                    continue  # Carried openings are reconstructed from roots, never trusted as roots.
                opening = _ledger_number(check.get('opening'))
                closing = _ledger_number(check.get('closing'))
                if opening + movement(start, end + timedelta(days=1)) != closing:
                    raise ValueError('Stored ledger arithmetic disagrees with current rows')
                if evidence.get('opening') is not None and _ledger_number(evidence['opening']) != opening:
                    raise ValueError('ERP opening disagrees with its stored proof')
                if evidence.get('closing') is not None and _ledger_number(evidence['closing']) != closing:
                    raise ValueError('ERP closing disagrees with its stored proof')
                anchors.append(dict(day=start, amount=opening, upload_id=upload['id'], source=check['opening_source']))
            for start, end in sorted(coverage):
                if components and start <= components[-1][1] + timedelta(days=1):
                    components[-1][1] = max(components[-1][1], end)
                else:
                    components.append([start, end])
        except (ValueError, TypeError, KeyError, AttributeError, OverflowError):
            issue('invalid_ledger_evidence', code, 'Ledger metadata or canonical amounts cannot support reliable balances; revalidate the source.')
            failed = True

        def boundary(day: date, period_key: str) -> tuple[Decimal | None, dict[str, Any] | None]:
            """A known opening must survive an unavailable later closing."""
            component = next((c for c in components if c[0] <= day <= c[1] + timedelta(days=1)), None)
            if failed or component is None or any(a <= component[1] and b >= component[0] for a, b in disputed):
                return None, None
            candidates = [a for a in anchors if component[0] <= a['day'] <= component[1]]
            if not candidates:
                issue('missing_current_ledger_anchor', code, 'No current independent opening supports this covered boundary.',
                      period_key=period_key, boundary_date=day.isoformat())
                return None, None
            amounts = {a['amount'] + movement(a['day'], day) for a in candidates}
            if len(amounts) != 1:
                issue('conflicting_ledger_anchors', code, 'Current source openings disagree across the covered ledger history.',
                      period_key=period_key, boundary_date=day.isoformat())
                return None, None
            return amounts.pop(), {'method': 'current_ledger_rollforward',
                                   'anchor_upload_ids': sorted(a['upload_id'] for a in candidates),
                                   'anchor_sources': {str(a['upload_id']): a['source'] for a in candidates},
                                   'coverage_start': component[0].isoformat(), 'coverage_end': component[1].isoformat()}

        records = []
        for period in normalized_periods:
            record = dict(account_code=code, account_name=name, treasury_type=treasury_type,
                          opening_balance=None, closing_balance=None, ledger_movement=None,
                          calculated_closing=None, status='Unavailable', provenance=None)
            component = next((c for c in components if c[0] <= period['start'] and c[1] >= period['end']), None)
            opening, opening_proof = boundary(period['start'], period['key'])
            closing, closing_proof = boundary(period['end'] + timedelta(days=1), period['key'])
            net = movement(period['start'], period['end'] + timedelta(days=1)) if not failed and component else None
            available = opening is not None and closing is not None and net is not None
            record.update(opening_balance=float(opening) if opening is not None else None,
                          closing_balance=float(closing) if closing is not None else None,
                          ledger_movement=float(net) if net is not None else None,
                          calculated_closing=float(opening + net) if opening is not None and net is not None else None,
                          status='Ready' if available else 'Partial' if opening is not None or closing is not None else 'Unavailable',
                          provenance={'opening': opening_proof, 'closing': closing_proof})
            if not failed and component is None:
                issue('ledger_coverage_gap', code, 'Complete ledger coverage does not span this reporting period.',
                      period_key=period['key'], period_start=period['start'].isoformat(), period_end=period['end'].isoformat(),
                      coverage=[{'start': a.isoformat(), 'end': b.isoformat()} for a, b in components[:20]])
            records.append(record)
        account_results[code] = records

    results = []
    calculated_opening: Decimal | None = None
    for index, period in enumerate(normalized_periods):
        accounts = [account_results[code][index] for code in account_types]
        complete = bool(accounts) and all(a['status'] == 'Ready' for a in accounts)
        def total(field: str) -> float | None:
            return (float(sum((_ledger_number(a[field]) for a in accounts), Decimal(0)))
                    if accounts and all(a[field] is not None for a in accounts) else None)
        opening, closing = total('opening_balance'), total('closing_balance')
        if index == 0 and opening is not None:
            calculated_opening = _ledger_number(opening)
        flow = cashflow_movements.get(period['key'])
        try:
            flow = _ledger_number(flow) if flow is not None else None
        except ValueError:
            flow = None
        if flow is None:
            diagnostics.append({'code': 'cashflow_movement_unavailable', 'period_key': period['key'],
                                'message': 'The Cashflow net movement is unavailable for this period.'})
        # Carry the Cashflow calculation across columns. Resetting each column
        # to its ledger opening would hide a previous period's discrepancy.
        calculated = calculated_opening + flow if calculated_opening is not None and flow is not None else None
        variance = _ledger_number(closing) - calculated if closing is not None and calculated is not None else None
        results.append(dict(key=period['key'], start=period['start'].isoformat(), end=period['end'].isoformat(),
                            accounts=accounts, complete=complete, opening_total=opening, closing_total=closing,
                            calculated_opening=float(calculated_opening) if calculated_opening is not None else None,
                            ledger_movement_total=total('ledger_movement'), cashflow_net_movement=float(flow) if flow is not None else None,
                            calculated_closing=float(calculated) if calculated is not None else None,
                            variance=float(variance) if variance is not None else None,
                            status='Unavailable' if not complete or variance is None else 'Matched' if abs(variance) <= Decimal('0.01') else 'Variance'))
        calculated_opening = calculated
    fingerprint = hashlib.sha256(json.dumps([uploads, rows, account_types, periods, cashflow_movements, account_names, treasury_movements],
                                           sort_keys=True, default=str).encode()).hexdigest()
    overall_complete = bool(results) and all(p['complete'] for p in results)
    overall_flow = sum((_ledger_number(p['cashflow_net_movement']) for p in results), Decimal(0)) if results and all(p['cashflow_net_movement'] is not None for p in results) else None
    overall_calculated = (_ledger_number(results[0]['opening_total']) + overall_flow
                          if results and results[0]['opening_total'] is not None and overall_flow is not None else None)
    overall_variance = (_ledger_number(results[-1]['closing_total']) - overall_calculated
                        if overall_calculated is not None and results[-1]['closing_total'] is not None else None)
    result = {'periods': results, 'coverage_complete': overall_complete,
            'ready': overall_complete and all(p['status'] != 'Unavailable' for p in results),
            'status': 'Unavailable' if not results or any(p['status'] == 'Unavailable' for p in results)
                      else 'Variance' if any(p['status'] == 'Variance' for p in results) else 'Matched',
            'overall': {'complete': overall_complete,
                        'opening_total': results[0]['opening_total'] if results else None,
                        'closing_total': results[-1]['closing_total'] if results else None,
                        'cashflow_net_movement': float(overall_flow) if overall_flow is not None else None,
                        'calculated_closing': float(overall_calculated) if overall_calculated is not None else None,
                        'variance': float(overall_variance) if overall_variance is not None else None},
            'evidence_fingerprint': fingerprint, 'diagnostics': diagnostics[:100], 'diagnostic_count': len(diagnostics)}
    if treasury_movements is not None:
        # Reuse the exact calculation on the same fetched snapshot. Split view
        # never borrows another connection or calculates balances in the browser.
        breakdown = {}
        for key, label in (('bank', 'Bank'), ('cash', 'Cash')):
            selected = {code: value for code, value in account_types.items() if value == label}
            part = build_uploaded_ledger_balances(
                [u for u in uploads if u.get('ledger_account_code') in selected],
                [r for r in rows if r.get('account_code') in selected], selected, periods,
                treasury_movements.get(key, {}), account_names=account_names)
            if not selected:
                # An explicitly empty configured type is not a missing ledger.
                for period in part['periods']:
                    period.update({field: 0.0 for field in ('opening_total', 'closing_total', 'calculated_opening',
                        'ledger_movement_total', 'cashflow_net_movement', 'calculated_closing', 'variance')})
                    period.update(complete=True, status='Not applicable')
                part['overall'].update({field: 0.0 for field in part['overall'] if field != 'complete'})
                part['overall']['complete'] = True
                part.update(ready=True, coverage_complete=True, status='Not applicable', diagnostics=[], diagnostic_count=0)
            breakdown[key] = part
        result['treasury_breakdown'] = breakdown
    return result


def load_uploaded_ledger_balances(
    client_id: int, account_types: Mapping[str, str], periods: Sequence[Mapping[str, Any]],
    cashflow_movements: Mapping[str, Any], *, query_fn: Callable[..., list[dict[str, Any]]] = execute_query_pg,
    account_names: Mapping[str, str] | None = None,
    treasury_movements: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Read compact metadata and projected canonical fields inside the caller's snapshot."""
    if not periods:
        return build_uploaded_ledger_balances([], [], account_types, periods, cashflow_movements,
                                              account_names=account_names, treasury_movements=treasury_movements)
    codes = list(account_types)
    uploads = query_fn("""
        SELECT u.id, u.template_id, u.ledger_account_code, u.ledger_account_name,
               u.ledger_period_start, u.ledger_period_end, u.ledger_evidence, t.ledger_columns,
               (SELECT json_agg(json_build_object('id', m.id, 'column_name', m.column_name,
                                                 'data_type', m.data_type) ORDER BY p.sort_order)
                FROM template_column_mappings p JOIN master_columns m ON m.id = p.column_id AND m.is_active = TRUE
                WHERE p.template_id = t.id) AS columns
        FROM uploaded_files u JOIN templates t ON t.id = u.template_id
        WHERE u.client_id = %s AND u.status = 'Imported' AND u.ledger_account_code = ANY(%s)
          AND t.data_purpose = 'account_ledger' AND t.is_active = TRUE ORDER BY u.id
        """, (client_id, codes)) if codes else []
    uploads = [dict(u) for u in uploads]
    for upload in uploads:
        try:
            roles = _ledger_json(upload['ledger_columns'])
            columns = _ledger_json(upload['columns']) or []
            current = hashlib.sha256(json.dumps([roles, columns], sort_keys=True).encode()).hexdigest()
            evidence = _ledger_json(upload['ledger_evidence']) or {}
            upload['configuration_current'] = current == evidence.get('balance_check', {}).get('configuration')
        except (ValueError, TypeError, KeyError, AttributeError):
            upload['configuration_current'] = False
    template_ids = sorted({u['template_id'] for u in uploads if u.get('configuration_current')})
    rows = query_fn("""
        SELECT cd.id, cd.version, cd.extract_date, cd.source_fingerprint,
               u.template_id, u.ledger_account_code AS account_code,
               cd.row_data::jsonb ->> debit.column_name AS debit,
               cd.row_data::jsonb ->> credit.column_name AS credit
        FROM client_data cd JOIN uploaded_files u
          ON u.id = cd.upload_id AND u.client_id = cd.client_id AND u.template_id = cd.template_id
        JOIN templates t ON t.id = cd.template_id AND t.data_purpose = 'account_ledger' AND t.is_active = TRUE
        JOIN master_columns debit ON debit.id = (t.ledger_columns::jsonb ->> 'debit')::integer
        JOIN master_columns credit ON credit.id = (t.ledger_columns::jsonb ->> 'credit')::integer
        WHERE cd.client_id = %s AND cd.is_deleted = FALSE AND u.status = 'Imported'
          AND u.ledger_account_code = ANY(%s) AND cd.template_id = ANY(%s) ORDER BY cd.id
        """, (client_id, codes, template_ids)) if template_ids else []
    return build_uploaded_ledger_balances(uploads, rows, account_types, periods, cashflow_movements,
                                          account_names=account_names, treasury_movements=treasury_movements)


def build_cashflow_differences(
    source: Mapping[str, Any], account_types: Mapping[str, str], period_start: str, period_end: str,
) -> dict[str, Any]:
    """Compare posting multisets without pairing corrections or changing reports.

    Two-decimal signed sides handle negative-side ERP reversals. Original values
    remain in evidence. Equal net movements never excuse differing occurrences.
    Ledger coverage reuses the existing independent proof, not a zero-movement
    claim: its movement variance is irrelevant to availability of source evidence.
    """
    start, end = date.fromisoformat(period_start), date.fromisoformat(period_end)
    if start > end or end == date.max:
        raise ValueError('Invalid differences reporting period')
    ledger, transactions = source['ledger_rows'], source['transaction_rows']
    period = {'key': 'selection', 'start': period_start, 'end': period_end}
    coverage = build_uploaded_ledger_balances(source['uploads'], ledger, account_types, [period], {'selection': 0})
    available = {a['account_code']: a['status'] == 'Ready' for a in coverage['periods'][0]['accounts']}
    for code in account_types:
        if any(not u.get('comparison_roles_available') for u in source['uploads'] if u.get('ledger_account_code') == code):
            available[code] = False
    groups = defaultdict(lambda: {'ledger': [], 'transaction': []})
    voucher_lines = defaultdict(list)
    for row in transactions:
        if _text(row.get('voucher_id')):
            voucher_lines[_text(row['voucher_id'])].append(row)

    def inside(row):
        try:
            return start <= date.fromisoformat(str(row.get('date'))[:10]) <= end
        except ValueError:
            return False

    def signature(row):
        day = date.fromisoformat(str(row.get('date'))[:10]).isoformat()
        if all(row.get(key) in (None, '') for key in ('debit', 'credit')):
            raise ValueError('Missing posting amount')
        debit, credit = (_ledger_number(row.get(key) or 0) for key in ('debit', 'credit'))
        # Preserve gross sides: reducing a debit/credit pair to net would hide offsets.
        normalized_debit = max(debit, Decimal(0)) + max(-credit, Decimal(0))
        normalized_credit = max(credit, Decimal(0)) + max(-debit, Decimal(0))
        return (day, normalized_debit.quantize(Decimal('.01')), normalized_credit.quantize(Decimal('.01')))

    for side, rows in (('ledger', ledger), ('transaction', transactions)):
        for row in rows:
            code = row.get('account_code')
            if code not in account_types:
                continue
            reference = _text(row.get('voucher_id'))
            # Blank references are separate evidence, never joined by amount/date.
            key = (code, reference, '' if reference else f"{side}:{row['id']}")
            groups[key][side].append(row)
    findings = []
    totals = {code: {'ledger': Decimal(0), 'transaction': Decimal(0)} for code in account_types}
    for (code, reference, unkeyed), sides in sorted(groups.items()):
        def invalid_date(row):
            try:
                date.fromisoformat(str(row.get('date'))[:10])
                return False
            except ValueError:
                return True
        if not any(inside(r) or invalid_date(r) for rows in sides.values() for r in rows):
            continue
        signatures, invalid = {}, False
        for side, rows in sides.items():
            signatures[side] = Counter()
            for row in rows:
                try:
                    sig = signature(row)
                    signatures[side][sig] += 1
                    if inside(row):
                        totals[code][side] += sig[1] - sig[2]
                except (ValueError, InvalidOperation):
                    invalid = True
        left, right = signatures['ledger'], signatures['transaction']
        changed_fields = []
        if left != right:
            if Counter(day for day, _, _ in left.elements()) != Counter(day for day, _, _ in right.elements()):
                changed_fields.append('dates')
            if Counter((debit, credit) for _, debit, credit in left.elements()) != Counter((debit, credit) for _, debit, credit in right.elements()):
                changed_fields.append('amounts')
            if len(sides['ledger']) != len(sides['transaction']):
                changed_fields.append('line_count')
        status = ('unavailable' if not available.get(code) else 'unresolved' if invalid or unkeyed
                  else 'transaction_only' if not sides['ledger'] else 'ledger_only' if not sides['transaction']
                  else 'matched' if left == right else 'changed')
        movements = {}
        for side, rows in sides.items():
            movements[side] = None if invalid else float(sum(
                (signature(row)[1] - signature(row)[2] for row in rows if inside(row)), Decimal(0)))
        def evidence_rows(rows):
            return [dict(row, in_period=inside(row)) for row in rows]
        findings.append(dict(account_code=code, treasury_type=account_types[code], voucher_id=reference,
            unkeyed_identity=unkeyed or None, status=status, difference_fields=changed_fields,
            ledger_line_count=len(sides['ledger']), transaction_line_count=len(sides['transaction']),
            matched_occurrences=sum((left & right).values()),
            ledger_movement=movements['ledger'], transaction_movement=movements['transaction'],
            movement_difference=(movements['ledger'] - movements['transaction']) if not invalid else None,
            ledger_lines=evidence_rows(sides['ledger']), transaction_lines=evidence_rows(sides['transaction']),
            voucher_lines=evidence_rows(voucher_lines.get(reference, [])),
            note=('Missing coverage or current configuration; absence cannot be established.' if status == 'unavailable'
                  else 'Reference/date/amount is insufficient for a reliable comparison.' if status == 'unresolved'
                  else 'Occurrence comparison only; line grouping can differ between extracts. No correction pairing is inferred.')))
    counts = dict(Counter(row['status'] for row in findings))
    posting_complete = bool(account_types) and all(available.values()) and not counts.get('unresolved')
    status = ('Unavailable' if not account_types or not all(available.values()) else 'Review required' if counts.get('unresolved')
              else 'Differences' if any(counts.get(k) for k in ('ledger_only', 'transaction_only', 'changed')) else 'Matched')
    invalid_accounts = {row['account_code'] for row in findings if row['ledger_movement'] is None}
    account_totals = [dict(account_code=code, treasury_type=account_types[code], evidence_available=available.get(code, False),
        ledger_movement=float(amounts['ledger']) if available.get(code) and code not in invalid_accounts else None,
        transaction_movement=float(amounts['transaction']) if code not in invalid_accounts else None,
        movement_difference=float(amounts['ledger'] - amounts['transaction']) if available.get(code) and code not in invalid_accounts else None)
        for code, amounts in totals.items()]
    return dict(status='Review required' if status == 'Matched' else status, posting_status=status,
        complete=False, posting_comparison_complete=posting_complete,
        transaction_coverage_status='Not independently certified',
        scope_note='Only current stored postings are compared. Transaction Report extraction completeness is not independently certified; source-only findings do not establish why a posting is absent.',
        period_start=period_start, period_end=period_end,
        counts=counts, account_totals=account_totals, findings=findings,
        ledger_sources=[{key: upload.get(key) for key in ('id', 'template_id', 'ledger_account_code',
            'ledger_account_name', 'ledger_period_start', 'ledger_period_end', 'uploaded_at')}
            for upload in source['uploads']],
        transaction_sources=[dict(upload_id=identity, uploaded_at=timestamp) for identity, timestamp in sorted(
            {(row.get('upload_id'), str(row.get('uploaded_at') or '')) for row in transactions}, key=lambda item: str(item[0]))],
        timestamp_note='Upload timestamps are import metadata, not certified ERP extraction times.',
        coverage_diagnostics=coverage['diagnostics'],
        evidence_fingerprint=hashlib.sha256(json.dumps([source, account_types, period], sort_keys=True, default=str).encode()).hexdigest(),
        amount_precision=2)


def _cashflow_gross_sides(rows, account_codes, periods):
    """Sum original posting sides; a blank/invalid source is never a zero proof."""
    totals = {p['key']: dict(receipts=Decimal(0), payments=Decimal(0)) for p in periods}
    invalid = set()
    for row in rows:
        if row.get('account_code') not in account_codes:
            continue
        try:
            day = date.fromisoformat(str(row.get('date', row.get('extract_date')))[:10])
        except ValueError:
            invalid.update(totals)
            continue
        key = next((p['key'] for p in periods if p['start'] <= day.isoformat() <= p['end']), None)
        if key is None:
            continue
        try:
            if all(row.get(field) in (None, '') for field in ('debit', 'credit')):
                raise ValueError('Missing posting amount')
            debit, credit = (_ledger_number(row.get(field) or 0) for field in ('debit', 'credit'))
        except (ValueError, InvalidOperation):
            invalid.add(key)
            continue
        totals[key]['receipts'] += max(debit, Decimal(0)) + max(-credit, Decimal(0))
        totals[key]['payments'] += max(credit, Decimal(0)) + max(-debit, Decimal(0))
    return totals, invalid


def build_cashflow_reconciliation(
    summary: Mapping[str, Any], source: Mapping[str, Any], account_types: Mapping[str, str],
    treasury_view: str = 'combined', *, include_posting_comparisons: bool = False,
) -> dict[str, Any]:
    """Reconcile independent gross ledger sides to unchanged categorized totals.

    Presentation entries are generated from explicit voucher operations by the
    summary engine. Source differences are retained separately, even if their
    net or presentation residuals cancel. No correction or balancing entry is
    generated from a ledger/report variance.
    """
    periods = summary['periods']
    if not periods:
        raise ValueError('A reconciliation requires reporting periods')
    explanation = build_cashflow_presentation_adjustments(summary, source['transaction_rows'], account_types, treasury_view)
    support = {}
    views = {}
    comparisons = {}
    def decimal(value):
        return Decimal(str(value))
    def output(value):
        if value is None:
            return None
        rounded = value.quantize(Decimal('.01'))
        return float(rounded) if rounded else 0.0
    for view, presentation in explanation['views'].items():
        selected = {code: kind for code, kind in account_types.items() if view == 'combined' or kind.casefold() == view}
        proof = {r['period_key']: r for r in summary['treasury_movement_proof'] if r['treasury_type'] == view}
        movements = {p['key']: proof[p['key']]['explained_movement']
                     if p['key'] in proof and proof[p['key']]['status'] == 'Matched' else None for p in periods}
        balances = build_uploaded_ledger_balances(source['uploads'], source['ledger_rows'], selected, periods, movements)
        comparison = build_cashflow_differences(source, selected, periods[0]['start'], periods[-1]['end'])
        if include_posting_comparisons:
            comparisons[view] = comparison
        ledger, invalid_ledger = _cashflow_gross_sides(source['ledger_rows'], selected, periods)
        transaction, invalid_transaction = _cashflow_gross_sides(source['transaction_rows'], selected, periods)
        roles_available = all(u.get('comparison_roles_available') for u in source['uploads']
                              if u.get('ledger_account_code') in selected)
        entries = presentation['entries']
        adjustments = {}
        for entry in entries:
            identity = hashlib.sha256(json.dumps([view, entry['reason'], entry['label'], entry['category']]).encode()).hexdigest()[:24]
            row = adjustments.setdefault(identity, dict(key=identity, reason=entry['reason'], label=entry['label'],
                category=entry['category'], periods={p['key']: dict(receipts=Decimal(0), payments=Decimal(0)) for p in periods}))
            for side in ('receipts', 'payments'):
                row['periods'][entry['period_key']][side] += decimal(entry[side])
            support.setdefault(identity, []).append(dict(entry, treasury_view=view))
        adjustment_rows = []
        for row in adjustments.values():
            row = dict(row)
            values = row['periods']
            row['total'] = {side: output(sum((p[side] for p in values.values()), Decimal(0))) for side in ('receipts', 'payments')}
            row['periods'] = {key: {side: output(value) for side, value in p.items()} for key, p in values.items()}
            row['support_count'] = len({(r['voucher_id'], r['period_key']) for r in support[row['key']]})
            adjustment_rows.append(row)
        results, unrounded_results = [], []
        for balance in balances['periods']:
            key = balance['key']
            available = balance['complete'] and roles_available and key not in invalid_ledger
            transaction_available = key not in invalid_transaction
            target = presentation['cashflow_totals'][key]
            sides, unrounded = {}, {}
            for side in ('receipts', 'payments'):
                raw = ledger[key][side] if available else None
                adjustment = sum((decimal(e[side]) for e in entries if e['period_key'] == key), Decimal(0))
                adjusted = raw + adjustment if raw is not None and transaction_available else None
                expected = decimal(target[side])
                unrounded[side] = dict(ledger=raw, transaction=transaction[key][side] if transaction_available else None,
                    adjustments=adjustment, adjusted_ledger=adjusted, cashflow=expected,
                    difference=adjusted - expected if adjusted is not None else None)
                sides[side] = {field: output(value) for field, value in unrounded[side].items()}
            unrounded_results.append(unrounded)
            movement = proof.get(key, {})
            opening = balance['opening_total']
            separate_transfer = movement.get('internal_transfer_movement')
            separate_mixed = movement.get('mixed_voucher_movement')
            calculated = (decimal(opening) + decimal(target['receipts']) - decimal(target['payments'])
                          + decimal(separate_transfer) + decimal(separate_mixed)
                          if all(value is not None for value in (opening, separate_transfer, separate_mixed)) else None)
            closing = balance['closing_total']
            amount_status = ('Unavailable' if any(s['difference'] is None for s in sides.values()) else
                             'Differences' if any(s['difference'] != 0 for s in sides.values()) else 'Matched')
            results.append(dict(key=key, start=balance['start'], end=balance['end'], receipts=sides['receipts'], payments=sides['payments'],
                amount_status=amount_status, balance_proof=dict(ledger_opening=opening, ledger_closing=closing,
                    separate_transfer_movement=separate_transfer, separate_mixed_movement=separate_mixed,
                    unallocated_category_movement=movement.get('unallocated_category_movement'),
                    calculated_closing=output(calculated), difference=output(decimal(closing) - calculated) if closing is not None and calculated is not None else None,
                    continuous_calculated_closing=output(decimal(balance['calculated_closing'])) if balance['calculated_closing'] is not None else None,
                    continuous_difference=output(decimal(balance['variance'])) if balance['variance'] is not None else None)))
        overall = {}
        for side in ('receipts', 'payments'):
            # Sum original precision before presentation rounding. Adding the
            # displayed columns would lose fractional-cent source amounts.
            overall[side] = {field: output(sum((p[side][field] for p in unrounded_results), Decimal(0)))
                            if all(p[side][field] is not None for p in unrounded_results) else None for field in results[0][side]}
        initial, final = balances['periods'][0]['opening_total'], balances['periods'][-1]['closing_total']
        total_transfers = sum((decimal(row['internal_transfer_movement']) for row in proof.values()), Decimal(0))
        total_mixed = sum((decimal(row['mixed_voucher_movement']) for row in proof.values()), Decimal(0))
        total_net = sum((p['receipts']['cashflow'] - p['payments']['cashflow'] for p in unrounded_results), Decimal(0))
        total_calculated = decimal(initial) + total_net + total_transfers + total_mixed if initial is not None else None
        overall_proof = dict(ledger_opening=initial, ledger_closing=final,
            separate_transfer_movement=output(total_transfers), separate_mixed_movement=output(total_mixed),
            calculated_closing=output(total_calculated),
            difference=output(decimal(final) - total_calculated) if final is not None and total_calculated is not None else None)
        amount_status = ('Unavailable' if any(p['amount_status'] == 'Unavailable' for p in results) else
                         'Differences' if any(p['amount_status'] == 'Differences' for p in results) else 'Matched')
        allocation_review = any(r.get('mixed_voucher_count') for r in proof.values())
        # Amount agreement is deliberately separate from occurrence and coverage
        # acceptance. An equal-and-opposite omission still requires attention.
        status = (amount_status if amount_status != 'Matched' else 'Review required')
        views[view] = dict(status=status, amount_status=amount_status, periods=results, overall=overall,
            adjustments=adjustment_rows, category_sections=presentation['category_sections'], overall_balance_proof=overall_proof,
            allocation_status='Review required' if allocation_review else 'Proven',
            unresolved=presentation['unresolved'], balance_controls=balances,
            treasury_movement_proof=list(proof.values()),
            posting_comparison={key: comparison[key] for key in ('posting_status', 'counts', 'account_totals',
                'transaction_coverage_status', 'posting_comparison_complete', 'evidence_fingerprint', 'coverage_diagnostics')})
        if presentation['unresolved'] and amount_status == 'Matched':
            views[view]['status'] = 'Review required'
    result = dict(treasury_view=treasury_view, periods=periods, group_by=summary['group_by'],
        receipt_presentation=summary['receipt_presentation'], views=views,
        review_controls=dict(category_review_count=summary['category_review_count'],
            customer_settlement_review_count=int(summary['customer_payment_details']['status'].ne('Matched').sum())),
        complete=False, transaction_coverage_status='Not independently certified',
        scope_note='Presentation agreement and posting agreement are separate. Completeness of the original Transaction Report extraction is not independently certified.',
        diagnostics=explanation['diagnostics'], _support_entries=support,
        evidence_fingerprint=hashlib.sha256(json.dumps([source, account_types, treasury_view,
            summary['reconciliation_evidence'], summary['receipt_presentation'], periods], sort_keys=True, default=str).encode()).hexdigest())
    if include_posting_comparisons:
        result['_posting_comparisons'] = comparisons
    return result
