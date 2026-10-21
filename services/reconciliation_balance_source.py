"""Normalize stored opening and month-end balances for reconciliation proofs."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from bisect import bisect_left
import hashlib
import json
import math
from typing import Any, Callable, Mapping, Sequence

from database import execute_query_pg


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
