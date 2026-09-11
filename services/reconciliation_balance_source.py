"""Normalize stored opening and month-end balances for reconciliation proofs."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
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
