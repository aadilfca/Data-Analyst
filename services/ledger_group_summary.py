"""Pure pandas engine for account-group ledger summaries.

The reporting UI and Excel exporter should call this module rather than
re-implementing voucher selection or debit/credit rules.  It deliberately has
no FastAPI, database, or file-system dependency so the accounting logic can be
tested with small in-memory data sets.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from numbers import Number
from typing import Any, Mapping, Sequence

import pandas as pd


DEFAULT_TOLERANCE = 0.005
CASHFLOW_GROUPINGS = ("day", "fortnight", "month", "quarter", "half_year", "year")


class LedgerBalanceError(ValueError):
    """Raised when a selected voucher does not balance across its two sides."""

    def __init__(self, message: str, checks: pd.DataFrame):
        super().__init__(message)
        self.checks = checks


def _canonical_code(value: Any) -> str:
    if value is None or pd.isna(value):
        return ""
    if isinstance(value, Number) and not isinstance(value, bool):
        numeric = float(value)
        if numeric.is_integer():
            return str(int(numeric))
    return str(value).strip()


def _clean_text(value: Any) -> str:
    if value is None or pd.isna(value):
        return ""
    return str(value).strip()


def _resolve_column(frame: pd.DataFrame, candidates: Sequence[str], required: bool = True) -> str | None:
    by_normalized = {str(column).strip().casefold(): column for column in frame.columns}
    for candidate in candidates:
        match = by_normalized.get(candidate.strip().casefold())
        if match is not None:
            return match
    if required:
        raise ValueError(f"Missing required column; expected one of: {', '.join(candidates)}")
    return None


def _rename_columns(frame: pd.DataFrame, aliases: Mapping[str, Sequence[str]], required: Sequence[str]) -> pd.DataFrame:
    rename_map: dict[str, str] = {}
    for canonical, candidates in aliases.items():
        actual = _resolve_column(frame, candidates, required=canonical in required)
        if actual is not None:
            rename_map[actual] = canonical
    return frame.rename(columns=rename_map).copy()


def _coerce_numeric(series: pd.Series, column_name: str) -> pd.Series:
    numeric = pd.to_numeric(series, errors="coerce")
    raw_text = series.map(_clean_text)
    invalid = numeric.isna() & raw_text.ne("")
    if invalid.any():
        examples = raw_text[invalid].head(3).tolist()
        raise ValueError(f"Column '{column_name}' contains non-numeric values: {examples}")
    return numeric.fillna(0.0).astype(float)


def _prepare_journal(journal_df: pd.DataFrame, period_start: pd.Timestamp, period_end: pd.Timestamp) -> pd.DataFrame:
    aliases = {
        "voucher_id": ("voucher_id", "Voucher ID", "voucher"),
        "date": ("date", "Date", "transaction_date", "Transaction Date"),
        "account_code": ("account_code", "Account Code", "account #", "Account #"),
        "account_name": ("account_name", "Account Name"),
        "voucher_detail": ("voucher_detail", "Voucher Detail", "detail", "Description"),
        "debit": ("debit", "Debit", "debit amount", "Debit Amount"),
        "credit": ("credit", "Credit", "credit amount", "Credit Amount"),
    }
    journal = _rename_columns(
        journal_df,
        aliases,
        required=("voucher_id", "date", "account_code", "debit", "credit"),
    )
    if "account_name" not in journal:
        journal["account_name"] = ""
    if "voucher_detail" not in journal:
        journal["voucher_detail"] = ""

    parsed_dates = pd.to_datetime(journal["date"], errors="coerce")
    invalid_dates = parsed_dates.isna() & journal["date"].map(_clean_text).ne("")
    if invalid_dates.any():
        examples = journal.loc[invalid_dates, "date"].head(3).tolist()
        raise ValueError(f"Column 'date' contains invalid dates: {examples}")

    journal["date"] = parsed_dates
    journal["voucher_id"] = journal["voucher_id"].map(_clean_text)
    journal["account_code"] = journal["account_code"].map(_canonical_code)
    journal["account_name"] = journal["account_name"].map(_clean_text)
    journal["voucher_detail"] = journal["voucher_detail"].map(_clean_text)
    journal["debit"] = _coerce_numeric(journal["debit"], "debit")
    journal["credit"] = _coerce_numeric(journal["credit"], "credit")
    journal = journal.loc[
        journal["voucher_id"].ne("")
        & journal["account_code"].ne("")
        & journal["date"].notna()
        & journal["date"].between(period_start, period_end, inclusive="both")
    ].copy()
    journal["month"] = journal["date"].dt.to_period("M").astype(str)
    return journal.reset_index(drop=True)


def _prepare_coa(coa_df: pd.DataFrame) -> pd.DataFrame:
    aliases = {
        "account_code": ("account_code", "Account Code", "account #", "Account #"),
        "account_title": ("account_title", "Account Title", "account_name", "Account Name"),
        "account_type": ("account_type", "Account Type", "type", "Type"),
        "category": ("category", "Category", "cat", "Cat"),
    }
    coa = _rename_columns(coa_df, aliases, required=("account_code", "category"))
    for optional in ("account_title", "account_type"):
        if optional not in coa:
            coa[optional] = ""
    coa["account_code"] = coa["account_code"].map(_canonical_code)
    coa["account_title"] = coa["account_title"].map(_clean_text)
    coa["account_type"] = coa["account_type"].map(_clean_text)
    coa["category"] = coa["category"].map(_clean_text)
    return coa.loc[coa["account_code"].ne("")].copy()


def _period_labels(period_start: pd.Timestamp, period_end: pd.Timestamp) -> list[str]:
    first = period_start.to_period("M")
    last = period_end.to_period("M")
    return [str(period) for period in pd.period_range(first, last, freq="M")]


def normalize_cashflow_grouping(value: Any) -> str:
    grouping = str(value or 'month').strip().casefold().replace('-', '_')
    aliases = {'halfyear': 'half_year', 'half_yearly': 'half_yearly', 'annual': 'year'}
    grouping = aliases.get(grouping, grouping)
    if grouping == 'half_yearly':
        grouping = 'half_year'
    if grouping not in CASHFLOW_GROUPINGS:
        allowed = ', '.join(CASHFLOW_GROUPINGS)
        raise ValueError('Unsupported Cashflows grouping {}. Choose one of: {}.'.format(value, allowed))
    return grouping


def _empty_internal_transfer_details() -> pd.DataFrame:
    return pd.DataFrame(
        columns=[
            "voucher_id",
            "date",
            "month",
            "period_key",
            "period_label",
            "transfer_type",
            "transferor_account_code",
            "transferor_account_name",
            "transferor_type",
            "transferee_account_code",
            "transferee_account_name",
            "transferee_type",
            "transferor_amount",
            "transferee_amount",
            "amount",
            "variance",
            "classification",
            "status",
            "matched",
            "voucher_detail",
        ]
    )


def _join_unique_leg_values(values: Any) -> str:
    seen: list[str] = []
    for value in values:
        text = _clean_text(value)
        if text and text not in seen:
            seen.append(text)
    return " / ".join(seen)


def build_cashflow_transfer_reconciliation(
    details: pd.DataFrame | None,
    *,
    tolerance: float = DEFAULT_TOLERANCE,
) -> dict[str, Any]:
    """Summarise transfer-source agreement without changing Cashflow totals."""
    if details is None or details.empty:
        return {
            "candidate_voucher_count": 0,
            "confirmed_count": 0,
            "mixed_count": 0,
            "ambiguous_count": 0,
            "transferor_total": 0.0,
            "transferee_total": 0.0,
            "variance_total": 0.0,
            "status": "No transfers",
        }

    def numeric_total(column: str) -> float:
        if column not in details.columns:
            return 0.0
        return float(pd.to_numeric(details[column], errors="coerce").fillna(0.0).sum())

    classifications = details.get("classification", pd.Series("", index=details.index)).fillna("").astype(str)
    candidate_count = int(len(details))
    confirmed_count = int(classifications.eq("confirmed").sum())
    mixed_count = int(classifications.eq("mixed").sum())
    ambiguous_count = int(classifications.eq("ambiguous").sum())
    variance_total = numeric_total("variance")
    matched = (
        candidate_count > 0
        and confirmed_count == candidate_count
        and abs(variance_total) <= tolerance
    )
    return {
        "candidate_voucher_count": candidate_count,
        "confirmed_count": confirmed_count,
        "mixed_count": mixed_count,
        "ambiguous_count": ambiguous_count,
        "transferor_total": numeric_total("transferor_amount"),
        "transferee_total": numeric_total("transferee_amount"),
        "variance_total": variance_total,
        "status": "Matched" if matched else "Review required",
    }


def _build_cashflow_transfer_details(
    working: pd.DataFrame,
    coa: pd.DataFrame,
    treasury_type: Mapping[str, str],
    voucher_order: pd.Index,
    *,
    match_internal_transfers: bool,
    tolerance: float,
) -> pd.DataFrame:
    """Identify treasury-to-treasury vouchers without changing summary totals."""
    if working.empty:
        return _empty_internal_transfer_details()

    treasury = working.loc[working["is_treasury"]].copy()
    if treasury.empty:
        return _empty_internal_transfer_details()
    treasury["treasury_type"] = treasury["account_code"].map(treasury_type).fillna("Treasury")
    account_title_by_code = (
        coa.drop_duplicates("account_code", keep="last")
        .set_index("account_code")["account_title"]
        .to_dict()
    )
    treasury["resolved_account_name"] = treasury["account_name"].where(
        treasury["account_name"].ne(""),
        treasury["account_code"].map(account_title_by_code).fillna(""),
    )
    treasury["net_movement"] = treasury["debit"] - treasury["credit"]
    account_legs = (
        treasury.groupby(["voucher_id", "account_code"], sort=False)
        .agg(
            account_name=("resolved_account_name", "first"),
            treasury_type=("treasury_type", "first"),
            debit=("debit", "sum"),
            credit=("credit", "sum"),
            net_movement=("net_movement", "sum"),
        )
        .reset_index()
    )
    candidate_ids = (
        account_legs.groupby("voucher_id", sort=False)["account_code"]
        .nunique()
        .loc[lambda values: values.gt(1)]
        .index
    )
    if len(candidate_ids) == 0:
        return _empty_internal_transfer_details()

    voucher_meta = (
        working.groupby("voucher_id", sort=False)
        .agg(
            date=("date", "min"),
            month=("month", "first"),
            period_key=("period_key", "first"),
            period_label=("period_label", "first"),
            voucher_detail=("voucher_detail", lambda values: next((value for value in values if value), "")),
        )
        .reindex(voucher_order)
        .loc[lambda frame: frame.index.isin(candidate_ids)]
    )
    debit_legs = account_legs.loc[
        account_legs["net_movement"].gt(tolerance) & account_legs["voucher_id"].isin(candidate_ids)
    ]
    credit_legs = account_legs.loc[
        account_legs["net_movement"].lt(-tolerance) & account_legs["voucher_id"].isin(candidate_ids)
    ]

    def aggregate_side(legs: pd.DataFrame, prefix: str) -> pd.DataFrame:
        if legs.empty:
            return pd.DataFrame(index=pd.Index([], name="voucher_id"))
        result = legs.groupby("voucher_id", sort=False).agg(
            account_code=("account_code", _join_unique_leg_values),
            account_name=("account_name", _join_unique_leg_values),
            treasury_type=("treasury_type", _join_unique_leg_values),
            amount=("net_movement", lambda values: float(values.abs().sum())),
            leg_count=("account_code", "nunique"),
        )
        return result.rename(
            columns={
                "account_code": f"{prefix}_account_code",
                "account_name": f"{prefix}_account_name",
                "treasury_type": f"{prefix}_type",
                "amount": f"{prefix}_amount",
                "leg_count": f"{prefix}_leg_count",
            }
        )

    details = voucher_meta.reset_index()
    details = details.merge(aggregate_side(credit_legs, "transferor"), on="voucher_id", how="left")
    details = details.merge(aggregate_side(debit_legs, "transferee"), on="voucher_id", how="left")
    non_treasury_counts = (
        working.loc[~working["is_treasury"]]
        .groupby("voucher_id", sort=False)
        .size()
        .rename("non_treasury_line_count")
    )
    details = details.merge(non_treasury_counts, on="voucher_id", how="left")
    for column in (
        "transferor_account_code",
        "transferor_account_name",
        "transferor_type",
        "transferee_account_code",
        "transferee_account_name",
        "transferee_type",
    ):
        details[column] = details[column].fillna("")
    for column in ("transferor_amount", "transferee_amount", "transferor_leg_count", "transferee_leg_count", "non_treasury_line_count"):
        details[column] = details[column].fillna(0.0)
    details["amount"] = details["transferee_amount"]
    details["variance"] = details["transferee_amount"] - details["transferor_amount"]
    details["transfer_type"] = (
        details["transferor_type"].replace("", "Unknown")
        + " to "
        + details["transferee_type"].replace("", "Unknown")
    )
    balanced = details["variance"].abs().le(tolerance)
    simple = details["transferor_leg_count"].eq(1) & details["transferee_leg_count"].eq(1)
    pure = details["non_treasury_line_count"].eq(0)
    details["classification"] = "ambiguous"
    details.loc[~pure, "classification"] = "mixed"
    details.loc[pure & simple & balanced, "classification"] = "confirmed"
    details["status"] = details["classification"].map(
        {
            "confirmed": "Confirmed",
            "mixed": "Review required - mixed voucher",
            "ambiguous": "Review required - multi-leg or unbalanced",
        }
    )
    same_type = (
        details["transferor_type"].ne("")
        & details["transferor_type"].eq(details["transferee_type"])
    )
    details["matched"] = (
        bool(match_internal_transfers)
        & same_type
        & details["classification"].eq("confirmed")
    )
    return details[
        [
            "voucher_id",
            "date",
            "month",
            "period_key",
            "period_label",
            "transfer_type",
            "transferor_account_code",
            "transferor_account_name",
            "transferor_type",
            "transferee_account_code",
            "transferee_account_name",
            "transferee_type",
            "transferor_amount",
            "transferee_amount",
            "amount",
            "variance",
            "classification",
            "status",
            "matched",
            "voucher_detail",
        ]
    ].sort_values(["date", "voucher_id"]).reset_index(drop=True)


def _period_descriptor(key: str, start: pd.Timestamp, end: pd.Timestamp, label: str) -> dict[str, str]:
    return {'key': key, 'start': start.date().isoformat(), 'end': end.date().isoformat(), 'label': label}


def _cashflow_periods(period_start: Any, period_end: Any, grouping: Any = 'month') -> list[dict[str, str]]:
    start = pd.Timestamp(period_start).normalize()
    end = pd.Timestamp(period_end).normalize()
    if pd.isna(start) or pd.isna(end) or start > end:
        raise ValueError('period_start and period_end must form a valid inclusive period')
    grouping = normalize_cashflow_grouping(grouping)
    periods: list[dict[str, str]] = []
    if grouping == 'day':
        candidates = [(day, day, day.strftime('%d-%b-%y'), day.strftime('%Y-%m-%d')) for day in pd.date_range(start, end, freq='D')]
    elif grouping == 'fortnight':
        candidates = []
        cursor = start.to_period('M')
        last_month = end.to_period('M')
        while cursor <= last_month:
            month_start = cursor.start_time.normalize()
            month_end = cursor.end_time.normalize()
            for sequence, bucket_start in enumerate((month_start, month_start + pd.Timedelta(days=14)), 1):
                bucket_end = min(bucket_start + pd.Timedelta(days=13), month_end)
                candidates.append((bucket_start, bucket_end, 'F{} {}'.format(sequence, bucket_start.strftime('%b-%y')), '{}-{}'.format(cursor, sequence)))
            cursor += 1
    elif grouping == 'month':
        candidates = [(period.start_time.normalize(), period.end_time.normalize(), period.start_time.strftime('%b-%y'), str(period)) for period in pd.period_range(start.to_period('M'), end.to_period('M'), freq='M')]
    elif grouping == 'quarter':
        candidates = [(period.start_time.normalize(), period.end_time.normalize(), 'Q{} {}'.format(period.quarter, period.year), '{}-Q{}'.format(period.year, period.quarter)) for period in pd.period_range(start.to_period('Q'), end.to_period('Q'), freq='Q')]
    elif grouping == 'half_year':
        candidates = []
        for year in range(start.year, end.year + 1):
            for half, month in ((1, 1), (2, 7)):
                bucket_start = pd.Timestamp(year=year, month=month, day=1)
                bucket_end = bucket_start + pd.offsets.DateOffset(months=6) - pd.Timedelta(days=1)
                candidates.append((bucket_start, bucket_end, 'H{} {}'.format(half, year), '{}-H{}'.format(year, half)))
    else:
        candidates = [(period.start_time.normalize(), period.end_time.normalize(), str(period.year), str(period.year)) for period in pd.period_range(start.to_period('Y'), end.to_period('Y'), freq='Y')]
    for candidate_start, candidate_end, label, key in candidates:
        clipped_start = max(start, candidate_start)
        clipped_end = min(end, candidate_end)
        if clipped_start <= clipped_end:
            periods.append(_period_descriptor(key, clipped_start, clipped_end, label))
    return periods


def _reindex_periods(frame: pd.DataFrame, period_keys: Sequence[str], index_name: str) -> pd.DataFrame:
    if frame.empty:
        empty = pd.DataFrame(index=pd.Index([], name=index_name), columns=list(period_keys), dtype=float)
        return empty.fillna(0.0)
    result = frame.reindex(columns=list(period_keys), fill_value=0.0).fillna(0.0)
    result.index.name = index_name
    return result.astype(float)

def _prepared_period_values(
    journal: pd.DataFrame,
    periods: Sequence[Mapping[str, str]],
    grouping: str,
) -> pd.Series | None:
    """Return prepared period assignments when every row has a valid key."""
    if grouping == "month":
        column = "transaction_month"
        key_map = {period["key"]: (period["key"], period["label"]) for period in periods}
    elif grouping == "fortnight":
        column = "transaction_fortnight"
        key_map = {
            f"{period['key'][:7]}-F{period['key'][-1]}": (period["key"], period["label"])
            for period in periods
        }
    else:
        return None
    if column not in journal.columns:
        return None
    prepared = journal[column].map(_clean_text)
    if prepared.empty or prepared.eq("").any():
        return None
    values = prepared.map(key_map)
    if values.isna().any():
        return None
    return values

def _reindex_months(frame: pd.DataFrame, month_labels: Sequence[str], index_name: str) -> pd.DataFrame:
    if frame.empty:
        empty = pd.DataFrame(index=pd.Index([], name=index_name), columns=list(month_labels), dtype=float)
        return empty.fillna(0.0)
    result = frame.reindex(columns=list(month_labels), fill_value=0.0).fillna(0.0)
    result.index.name = index_name
    return result.astype(float)


def _display_category_map(category_values: Sequence[str]) -> dict[str, str]:
    variants: dict[str, Counter[str]] = defaultdict(Counter)
    first_seen: dict[tuple[str, str], int] = {}
    for position, value in enumerate(category_values):
        cleaned = _clean_text(value)
        if not cleaned:
            continue
        key = cleaned.casefold()
        variants[key][cleaned] += 1
        first_seen.setdefault((key, cleaned), position)
    result: dict[str, str] = {}
    for key, counts in variants.items():
        result[key] = min(
            counts,
            key=lambda text: (-counts[text], first_seen.get((key, text), 0), text.casefold(), text),
        )
    return result


def _empty_balance_checks() -> pd.DataFrame:
    return pd.DataFrame(
        columns=["voucher_id", "group_net", "contra_net", "variance", "balanced"]
    )


def _value_for_period(values: Mapping[Any, Any], month_label: str) -> float | None:
    period = pd.Period(month_label, freq="M")
    candidate_keys: list[Any] = [month_label, period, str(period), period.to_timestamp(), period.to_timestamp().date()]
    for key in candidate_keys:
        if key in values:
            value = values[key]
            return None if value is None or pd.isna(value) else float(value)
    return None


def _build_reconciliation(
    group_account_pivot: pd.DataFrame,
    group_account_codes: Sequence[str],
    month_labels: Sequence[str],
    opening_balances: Mapping[str, Any] | None,
    actual_closing_balances: Mapping[str, Mapping[Any, Any]] | None,
    tolerance: float,
) -> tuple[pd.DataFrame, pd.DataFrame, bool, str | None]:
    proof_columns = [
        "month",
        "net_movement",
        "derived_closing",
        "actual_closing_balance",
        "variance",
        "status",
    ]
    by_account_columns = [
        "account_code",
        "month",
        "net_movement",
        "derived_closing",
        "actual_closing_balance",
        "variance",
        "status",
    ]
    if opening_balances is None or actual_closing_balances is None:
        return (
            pd.DataFrame(columns=proof_columns),
            pd.DataFrame(columns=by_account_columns),
            False,
            "Opening balances and actual closing balances are required for reconciliation proof.",
        )

    normalized_openings = {_canonical_code(code): float(value or 0) for code, value in opening_balances.items()}
    normalized_actuals = {
        _canonical_code(code): values for code, values in actual_closing_balances.items()
    }
    missing_openings = [code for code in group_account_codes if code not in normalized_openings]
    missing_actuals = [code for code in group_account_codes if code not in normalized_actuals]
    if missing_openings or missing_actuals:
        missing_parts = []
        if missing_openings:
            missing_parts.append(f"opening balances missing: {missing_openings}")
        if missing_actuals:
            missing_parts.append(f"actual closing balances missing: {missing_actuals}")
        return (
            pd.DataFrame(columns=proof_columns),
            pd.DataFrame(columns=by_account_columns),
            False,
            "; ".join(missing_parts),
        )

    account_rows: list[dict[str, Any]] = []
    for account_code in group_account_codes:
        closing = normalized_openings.get(account_code, 0.0)
        movements = group_account_pivot.loc[account_code] if account_code in group_account_pivot.index else pd.Series(dtype=float)
        for month_label in month_labels:
            movement = float(movements.get(month_label, 0.0))
            closing += movement
            actual = _value_for_period(normalized_actuals[account_code], month_label)
            variance = None if actual is None else closing - actual
            account_rows.append(
                {
                    "account_code": account_code,
                    "month": month_label,
                    "net_movement": movement,
                    "derived_closing": closing,
                    "actual_closing_balance": actual,
                    "variance": variance,
                    "status": "Matched" if actual is not None and abs(variance) <= tolerance else "Variance",
                }
            )

    account_frame = pd.DataFrame(account_rows, columns=by_account_columns)
    combined_opening = sum(normalized_openings.get(code, 0.0) for code in group_account_codes)
    combined_rows: list[dict[str, Any]] = []
    combined_closing = combined_opening
    for month_label in month_labels:
        movement = float(group_account_pivot[month_label].sum()) if month_label in group_account_pivot else 0.0
        combined_closing += movement
        actual_values = [
            _value_for_period(normalized_actuals[code], month_label) for code in group_account_codes
        ]
        actual = None if any(value is None for value in actual_values) else float(sum(actual_values))
        variance = None if actual is None else combined_closing - actual
        combined_rows.append(
            {
                "month": month_label,
                "net_movement": movement,
                "derived_closing": combined_closing,
                "actual_closing_balance": actual,
                "variance": variance,
                "status": "Matched" if actual is not None and abs(variance) <= tolerance else "Variance",
            }
        )
    return pd.DataFrame(combined_rows, columns=proof_columns), account_frame, True, None


def build_cross_group_check(
    left_summary: Mapping[str, Any],
    right_summary: Mapping[str, Any],
    left_category: str,
    right_category: str,
    right_multiplier: float = -1.0,
    tolerance: float = DEFAULT_TOLERANCE,
) -> pd.DataFrame:
    """Compare one category from two group summaries month by month."""

    months = list(left_summary.get("months", []))
    right_months = set(right_summary.get("months", []))
    months.extend(month for month in right_summary.get("months", []) if month not in months)
    left_pivot = left_summary.get("category_pivot", pd.DataFrame())
    right_pivot = right_summary.get("category_pivot", pd.DataFrame())
    rows = []
    for month in months:
        left_value = float(left_pivot.loc[left_category, month]) if left_category in left_pivot.index and month in left_pivot else 0.0
        right_value = float(right_pivot.loc[right_category, month]) if right_category in right_pivot.index and month in right_pivot else 0.0
        right_equivalent = right_value * right_multiplier
        variance = left_value - right_equivalent
        rows.append(
            {
                "month": month,
                "left_value": left_value,
                "right_equivalent": right_equivalent,
                "variance": variance,
                "status": "Matched" if abs(variance) <= tolerance else "Variance",
            }
        )
    return pd.DataFrame(rows, columns=["month", "left_value", "right_equivalent", "variance", "status"])


def build_financial_statement_summary(
    journal_df: pd.DataFrame,
    coa_df: pd.DataFrame,
    period_start: Any,
    period_end: Any,
) -> dict[str, Any]:
    """Aggregate all journal lines by central Financial Statement category.

    This preview deliberately reports transparent net movement (debit minus credit).
    Statement-specific normal-balance presentation is a later reporting concern.
    """

    start = pd.Timestamp(period_start)
    end = pd.Timestamp(period_end)
    if pd.isna(start) or pd.isna(end) or start > end:
        raise ValueError("period_start and period_end must form a valid inclusive period")
    months = _period_labels(start, end)
    journal = _prepare_journal(journal_df, start, end)
    coa = _prepare_coa(coa_df)
    category_by_code = (
        coa.drop_duplicates("account_code", keep="last")
        .set_index("account_code")["category"]
        .to_dict()
    )
    journal["category"] = journal["account_code"].map(category_by_code).fillna("")
    journal["signed_movement"] = journal["debit"] - journal["credit"]
    mapped = journal.loc[journal["category"].ne("")].copy()
    if mapped.empty:
        category_totals = pd.DataFrame(index=pd.Index([], name="category"), columns=[*months, "Total"], dtype=float)
    else:
        pivot = mapped.pivot_table(index="category", columns="month", values="signed_movement", aggfunc="sum", fill_value=0.0)
        category_totals = _reindex_months(pivot.sort_index(), months, "category")
        category_totals["Total"] = category_totals.sum(axis=1)
    account_counts = (
        mapped.groupby("category")["account_code"].nunique().sort_index()
        if not mapped.empty else pd.Series(dtype=int, name="account_count")
    )
    unmapped = journal.loc[journal["category"].eq(""), ["account_code", "account_name", "debit", "credit"]].copy()
    if not unmapped.empty:
        unmapped = (unmapped.groupby(["account_code", "account_name"], dropna=False, as_index=False)[["debit", "credit"]].sum())
        unmapped["net_movement"] = unmapped["debit"] - unmapped["credit"]
        unmapped = unmapped.sort_values(["account_code", "account_name"])
    return {
        "months": months,
        "category_totals": category_totals,
        "account_counts": account_counts,
        "unmapped_accounts": unmapped,
        "selected_voucher_count": int(journal["voucher_id"].nunique()),
        "source_line_count": int(len(journal)),
        "mapped_account_count": int(mapped["account_code"].nunique()),
        "unmapped_account_count": int(unmapped["account_code"].nunique()) if not unmapped.empty else 0,
    }


def build_cashflow_summary(
    journal_df: pd.DataFrame,
    coa_df: pd.DataFrame,
    cash_account_codes: Sequence[str],
    bank_account_codes: Sequence[str],
    period_start: Any,
    period_end: Any,
    *,
    group_by: str = 'month',
    match_internal_transfers: bool = False,
    receipt_presentation: str = "net",
    tolerance: float = DEFAULT_TOLERANCE,
) -> dict[str, Any]:
    """Summarise vouchers containing configured Cash or Bank accounts.

    Cash/Bank lines define the treasury leg. Counter-account lines are grouped
    by the Cash Flow category assigned in Account Control Centre. Customer
    receipts default to net presentation: charge and withholding lines remain
    available in ``deduction_details`` while the main customer category uses
    the net treasury effect.
    """
    start = pd.Timestamp(period_start)
    end = pd.Timestamp(period_end)
    if pd.isna(start) or pd.isna(end) or start > end:
        raise ValueError("period_start and period_end must form a valid inclusive period")
    presentation = str(receipt_presentation or "net").strip().casefold()
    if presentation not in {"net", "gross"}:
        raise ValueError("receipt_presentation must be net or gross")

    grouping = normalize_cashflow_grouping(group_by)
    periods = _cashflow_periods(start, end, grouping)
    period_keys = [period["key"] for period in periods]
    period_lookup: dict[pd.Timestamp, tuple[str, str]] = {}
    for period in periods:
        for day in pd.date_range(period["start"], period["end"], freq="D"):
            period_lookup[day.normalize()] = (period["key"], period["label"])

    months = _period_labels(start, end)
    journal = _prepare_journal(journal_df, start, end)
    if not journal.empty:
        period_values = _prepared_period_values(journal, periods, grouping)
        if period_values is None:
            period_values = journal["date"].dt.normalize().map(period_lookup)
        if period_values.isna().any():
            raise ValueError("A transaction date did not fall inside the requested reporting periods")
        period_frame = pd.DataFrame(
            period_values.tolist(),
            index=journal.index,
            columns=["period_key", "period_label"],
        )
        journal[["period_key", "period_label"]] = period_frame
    coa = _prepare_coa(coa_df)
    category_by_code = (
        coa.drop_duplicates("account_code", keep="last").set_index("account_code")["category"].to_dict()
    )
    cash_codes = {_canonical_code(code) for code in cash_account_codes if _canonical_code(code)}
    bank_codes = {_canonical_code(code) for code in bank_account_codes if _canonical_code(code)}
    treasury_codes = cash_codes | bank_codes
    treasury_type = {code: "Cash" for code in cash_codes}
    treasury_type.update({code: "Bank" for code in bank_codes})
    selected_vouchers = journal.loc[journal["account_code"].isin(treasury_codes), "voucher_id"].drop_duplicates().tolist()
    working = journal.loc[journal["voucher_id"].isin(selected_vouchers)].copy()

    def is_customer_category(label: str) -> bool:
        value = label.casefold()
        return "customer" in value and ("receipt" in value or "payment" in value)

    if working.empty:
        balance_checks = pd.DataFrame(columns=["voucher_id", "treasury_net", "counter_net", "variance", "balanced"])
        transfer_details = _empty_internal_transfer_details()
        category_frame = pd.DataFrame(columns=["category", "month", "period_key", "amount"])
        review = pd.DataFrame(columns=["voucher_id", "date", "month", "period_key", "period_label", "account_code", "account_name", "voucher_detail", "debit", "credit", "fallback_category"])
        deductions = pd.DataFrame(columns=["category", "month", "period_key", "period_label", "amount", "account_code", "account_name", "date", "presentation"])
    else:
        working["is_treasury"] = working["account_code"].isin(treasury_codes)
        working["treasury_movement"] = (working["debit"] - working["credit"]).where(working["is_treasury"], 0.0)
        working["counter_movement"] = (working["credit"] - working["debit"]).where(~working["is_treasury"], 0.0)
        voucher_order = pd.Index(working["voucher_id"].drop_duplicates(), name="voucher_id")
        balance_checks = working.groupby("voucher_id", sort=False)[["treasury_movement", "counter_movement"]].sum()
        balance_checks = balance_checks.rename(columns={"treasury_movement": "treasury_net", "counter_movement": "counter_net"}).reindex(voucher_order)
        balance_checks["variance"] = balance_checks["treasury_net"] - balance_checks["counter_net"]
        balance_checks["balanced"] = balance_checks["variance"].abs() <= tolerance
        balance_checks = balance_checks.reset_index()

        transfer_details = _build_cashflow_transfer_details(
            working,
            coa,
            treasury_type,
            voucher_order,
            match_internal_transfers=match_internal_transfers,
            tolerance=tolerance,
        )

        contra = working.loc[~working["is_treasury"]].copy()
        mapped_categories = contra["account_code"].map(category_by_code).fillna("")
        contra["category"] = mapped_categories.where(
            mapped_categories.ne(""),
            contra["account_name"].where(contra["account_name"].ne(""), "Unmapped account"),
        )
        contra["amount"] = contra["credit"] - contra["debit"]
        category_text = contra["category"].fillna("").astype(str).str.casefold()
        contra["is_customer"] = category_text.str.contains("customer", regex=False) & (
            category_text.str.contains("receipt", regex=False) | category_text.str.contains("payment", regex=False)
        )
        contra["is_deduction"] = (
            category_text.str.contains("charge", regex=False)
            | category_text.str.contains("withholding", regex=False)
            | category_text.str.contains("wht", regex=False)
            | category_text.str.contains("deduction", regex=False)
        )
        review = contra.loc[
            mapped_categories.eq(""),
            ["voucher_id", "date", "month", "period_key", "period_label", "account_code", "account_name", "voucher_detail", "debit", "credit", "category"],
        ].rename(columns={"category": "fallback_category"})

        has_customer = contra.groupby("voucher_id", sort=False)["is_customer"].any().reindex(voucher_order, fill_value=False).to_numpy()
        has_deduction = contra.groupby("voucher_id", sort=False)["is_deduction"].any().reindex(voucher_order, fill_value=False).to_numpy()
        net_vouchers = voucher_order[has_customer & has_deduction] if presentation == "net" else pd.Index([], dtype=object)
        net_special = contra["voucher_id"].isin(net_vouchers) & (contra["is_customer"] | contra["is_deduction"])
        category_lines = contra.loc[~net_special, ["category", "month", "period_key", "period_label", "amount"]]
        if len(net_vouchers):
            special = contra.loc[net_special]
            net_rows = special.loc[special["is_customer"]].drop_duplicates("voucher_id", keep="first")[["voucher_id", "category", "month", "period_key", "period_label"]].copy()
            net_amounts = special.groupby("voucher_id", sort=False)["amount"].sum()
            net_rows["amount"] = net_rows["voucher_id"].map(net_amounts)
            category_frame = pd.concat([category_lines, net_rows[["category", "month", "period_key", "period_label", "amount"]]], ignore_index=True)
        else:
            category_frame = category_lines.copy()

        deduction_rows = contra.loc[contra["is_deduction"], ["voucher_id", "category", "month", "period_key", "period_label", "amount", "account_code", "account_name", "date"]].copy()
        deduction_rows["presentation"] = "shown_separately"
        if len(net_vouchers):
            deduction_rows.loc[deduction_rows["voucher_id"].isin(net_vouchers), "presentation"] = "netted_from_customer_receipt"
        deductions = deduction_rows.drop(columns=["voucher_id"])

    unbalanced = balance_checks.loc[~balance_checks["balanced"]] if not balance_checks.empty else balance_checks
    if not unbalanced.empty:
        examples = unbalanced[["voucher_id", "variance"]].head(5).to_dict("records")
        raise LedgerBalanceError(f"Cash/Bank selection contains {len(unbalanced)} unbalanced voucher(s); examples: {examples}", balance_checks)

    if category_frame.empty:
        category_totals = pd.DataFrame(index=pd.Index([], name="category"), columns=[*period_keys, "Total"], dtype=float)
    else:
        pivot = category_frame.pivot_table(index="category", columns="period_key", values="amount", aggfunc="sum", fill_value=0.0)
        category_totals = _reindex_periods(pivot.sort_index(), period_keys, "category")
        category_totals["Total"] = category_totals.sum(axis=1)
    review = review.reset_index(drop=True)
    deductions = deductions[["category", "month", "period_key", "period_label", "amount", "account_code", "account_name", "date", "presentation"]].reset_index(drop=True)
    transfer_reconciliation = build_cashflow_transfer_reconciliation(transfer_details, tolerance=tolerance)
    period_metrics = {
        period["key"]: {
            "source_line_count": int((working["period_key"] == period["key"]).sum()) if not working.empty else 0,
            "selected_voucher_count": int(working.loc[working["period_key"] == period["key"], "voucher_id"].nunique()) if not working.empty else 0,
            "internal_transfer_count": int((transfer_details["period_key"] == period["key"]).sum()) if not transfer_details.empty else 0,
            "category_review_count": int((review["period_key"] == period["key"]).sum()) if not review.empty else 0,
        }
        for period in periods
    }
    return {
        "months": months,
        "periods": periods,
        "group_by": grouping,
        "category_totals": category_totals,
        "transfer_details": transfer_details,
        "transfer_reconciliation": transfer_reconciliation,
        "deduction_details": deductions,
        "categories_needing_review": review,
        "balance_checks": balance_checks,
        "selected_vouchers": selected_vouchers,
        "selected_voucher_count": len(selected_vouchers),
        "source_line_count": len(working),
        "internal_transfer_count": len(transfer_details),
        "category_review_count": len(review),
        "period_metrics": period_metrics,
        "receipt_presentation": presentation,
    }

def build_group_summary(
    journal_df: pd.DataFrame,
    coa_df: pd.DataFrame,
    group_account_codes: Sequence[str],
    period_start: Any,
    period_end: Any,
    *,
    group_label: str = "Group",
    related_group_codes: Sequence[str] | None = None,
    related_group_label: str | None = None,
    related_group_category: str | None = None,
    opening_balances: Mapping[str, Any] | None = None,
    actual_closing_balances: Mapping[str, Mapping[Any, Any]] | None = None,
    tolerance: float = DEFAULT_TOLERANCE,
) -> dict[str, Any]:
    """Build a category-wise summary for one explicit account-code group.

    The selected voucher set is determined by account code, then every ledger
    line for those vouchers is retained.  Group movement uses ``debit-credit``;
    contra movement uses ``credit-debit``.  These equal by double-entry, and a
    non-zero per-voucher variance raises ``LedgerBalanceError`` before any
    summary is returned.
    """

    start = pd.Timestamp(period_start)
    end = pd.Timestamp(period_end)
    if pd.isna(start) or pd.isna(end) or start > end:
        raise ValueError("period_start and period_end must form a valid inclusive period")
    group_codes = list(dict.fromkeys(_canonical_code(code) for code in group_account_codes if _canonical_code(code)))
    if not group_codes:
        raise ValueError("group_account_codes must contain at least one account code")
    related_codes = list(dict.fromkeys(_canonical_code(code) for code in (related_group_codes or []) if _canonical_code(code)))
    month_labels = _period_labels(start, end)

    journal = _prepare_journal(journal_df, start, end)
    coa = _prepare_coa(coa_df)
    category_by_code = (
        coa.drop_duplicates("account_code", keep="last")
        .set_index("account_code")["category"]
        .to_dict()
    )
    coa_category_display = _display_category_map(coa["category"].tolist())

    selected_vouchers = journal.loc[journal["account_code"].isin(group_codes), "voucher_id"].drop_duplicates().tolist()
    working = journal.loc[journal["voucher_id"].isin(selected_vouchers)].copy()
    working["is_group_leg"] = working["account_code"].isin(group_codes)
    group_lines = working.loc[working["is_group_leg"]].copy()
    contra_lines = working.loc[~working["is_group_leg"]].copy()
    group_lines["signed_movement"] = group_lines["debit"] - group_lines["credit"]
    contra_lines["signed_movement"] = contra_lines["credit"] - contra_lines["debit"]

    balance_rows: list[dict[str, Any]] = []
    contra_by_voucher = contra_lines.groupby("voucher_id", sort=False)
    group_by_voucher = group_lines.groupby("voucher_id", sort=False)
    for voucher_id in selected_vouchers:
        group_net = float(group_by_voucher.get_group(voucher_id)["signed_movement"].sum()) if voucher_id in group_by_voucher.groups else 0.0
        contra_net = float(contra_by_voucher.get_group(voucher_id)["signed_movement"].sum()) if voucher_id in contra_by_voucher.groups else 0.0
        variance = group_net - contra_net
        balance_rows.append(
            {
                "voucher_id": voucher_id,
                "group_net": group_net,
                "contra_net": contra_net,
                "variance": variance,
                "balanced": abs(variance) <= tolerance,
            }
        )
    balance_checks = pd.DataFrame(balance_rows) if balance_rows else _empty_balance_checks()
    unbalanced = balance_checks.loc[~balance_checks["balanced"]] if not balance_checks.empty else balance_checks
    if not unbalanced.empty:
        examples = unbalanced[["voucher_id", "variance"]].head(5).to_dict("records")
        raise LedgerBalanceError(
            f"Selected ledger contains {len(unbalanced)} unbalanced voucher(s); examples: {examples}",
            balance_checks,
        )

    category_values: list[str] = []
    categories_needing_review_rows: list[dict[str, Any]] = []
    related_label = related_group_category or (
        f"Received/Paid to {related_group_label}" if related_group_label else "Related Group"
    )
    contra_source_categories: list[str] = []
    for row_index, row in contra_lines.iterrows():
        account_code = row["account_code"]
        if account_code in related_codes:
            raw_category = related_label
        elif account_code in category_by_code and category_by_code[account_code]:
            raw_category = category_by_code[account_code]
        else:
            raw_category = row["account_name"] or "Unmapped account"
            categories_needing_review_rows.append(
                {
                    "voucher_id": row["voucher_id"],
                    "date": row["date"],
                    "month": row["month"],
                    "account_code": account_code,
                    "account_name": row["account_name"],
                    "voucher_detail": row["voucher_detail"],
                    "debit": row["debit"],
                    "credit": row["credit"],
                    "fallback_category": raw_category,
                }
            )
        contra_lines.at[row_index, "raw_category"] = raw_category
        contra_source_categories.append(raw_category)
        category_values.append(raw_category)

    display_map = _display_category_map(list(coa_category_display.values()) + category_values)
    if "raw_category" not in contra_lines:
        contra_lines["raw_category"] = pd.Series(index=contra_lines.index, dtype="object")
    contra_lines["category"] = contra_lines["raw_category"].map(
        lambda value: display_map.get(_clean_text(value).casefold(), _clean_text(value))
    )
    categories_needing_review = pd.DataFrame(
        categories_needing_review_rows,
        columns=[
            "voucher_id",
            "date",
            "month",
            "account_code",
            "account_name",
            "voucher_detail",
            "debit",
            "credit",
            "fallback_category",
        ],
    )

    category_pivot_raw = contra_lines.pivot_table(
        index="category", columns="month", values="signed_movement", aggfunc="sum", fill_value=0.0
    )
    category_pivot = _reindex_months(category_pivot_raw.sort_index(), month_labels, "category")
    category_totals = category_pivot.copy()
    category_totals["Total"] = category_totals.sum(axis=1)
    group_account_pivot_raw = group_lines.pivot_table(
        index="account_code", columns="month", values="signed_movement", aggfunc="sum", fill_value=0.0
    )
    group_account_pivot = _reindex_months(group_account_pivot_raw.sort_index(), month_labels, "account_code")

    category_monthly = category_pivot.sum(axis=0)
    group_monthly = group_account_pivot.sum(axis=0)
    monthly_balance_checks = pd.DataFrame(
        {
            "month": month_labels,
            "category_total": [float(category_monthly.get(month, 0.0)) for month in month_labels],
            "group_total": [float(group_monthly.get(month, 0.0)) for month in month_labels],
        }
    )
    monthly_balance_checks["variance"] = (
        monthly_balance_checks["category_total"] - monthly_balance_checks["group_total"]
    )
    monthly_balance_checks["balanced"] = monthly_balance_checks["variance"].abs() <= tolerance

    internal_voucher_ids = [
        voucher_id
        for voucher_id in selected_vouchers
        if voucher_id not in contra_by_voucher.groups
    ]
    internal_transfers = group_lines.loc[group_lines["voucher_id"].isin(internal_voucher_ids)].copy()
    internal_transfers = internal_transfers[
        [
            "voucher_id",
            "date",
            "month",
            "account_code",
            "account_name",
            "voucher_detail",
            "debit",
            "credit",
            "signed_movement",
        ]
    ].sort_values(["date", "voucher_id", "account_code"])
    internal_transfer_pivot = _reindex_months(
        internal_transfers.pivot_table(
            index="account_code", columns="month", values="signed_movement", aggfunc="sum", fill_value=0.0
        ),
        month_labels,
        "account_code",
    )

    related_lines = contra_lines.loc[contra_lines["account_code"].isin(related_codes)].copy()
    related_group_breakdown = related_lines[
        ["voucher_id", "date", "month", "account_code", "account_name", "voucher_detail", "debit", "credit", "signed_movement"]
    ].sort_values(["date", "voucher_id", "account_code"])
    related_group_pivot = _reindex_months(
        related_lines.pivot_table(
            index="account_code", columns="month", values="signed_movement", aggfunc="sum", fill_value=0.0
        ),
        month_labels,
        "account_code",
    )

    reconciliation_proof, reconciliation_by_account, reconciliation_available, reconciliation_message = _build_reconciliation(
        group_account_pivot,
        group_codes,
        month_labels,
        opening_balances,
        actual_closing_balances,
        tolerance,
    )

    return {
        "group_label": group_label,
        "period_start": start,
        "period_end": end,
        "months": month_labels,
        "group_account_codes": group_codes,
        "related_group_codes": related_codes,
        "selected_vouchers": selected_vouchers,
        "working_rows": working,
        "group_lines": group_lines,
        "contra_lines": contra_lines,
        "category_pivot": category_pivot,
        "category_totals": category_totals,
        "group_account_pivot": group_account_pivot,
        "balance_checks": balance_checks,
        "monthly_balance_checks": monthly_balance_checks,
        "internal_transfers": internal_transfers,
        "internal_transfer_pivot": internal_transfer_pivot,
        "related_group_breakdown": related_group_breakdown,
        "related_group_pivot": related_group_pivot,
        "categories_needing_review": categories_needing_review,
        "reconciliation_proof": reconciliation_proof,
        "reconciliation_by_account": reconciliation_by_account,
        "reconciliation_available": reconciliation_available,
        "reconciliation_message": reconciliation_message,
    }
