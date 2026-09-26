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

    months = _period_labels(start, end)
    journal = _prepare_journal(journal_df, start, end)
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
    category_rows: list[dict[str, Any]] = []
    transfer_rows: list[dict[str, Any]] = []
    deduction_rows: list[dict[str, Any]] = []
    review_rows: list[dict[str, Any]] = []
    balance_rows: list[dict[str, Any]] = []

    def is_customer_category(label: str) -> bool:
        value = label.casefold()
        return "customer" in value and ("receipt" in value or "payment" in value)

    def is_deduction_category(label: str) -> bool:
        value = label.casefold()
        return "charge" in value or "withholding" in value or "wht" in value or "deduction" in value

    for voucher_id, voucher in working.groupby("voucher_id", sort=False):
        treasury = voucher.loc[voucher["account_code"].isin(treasury_codes)].copy()
        contra = voucher.loc[~voucher["account_code"].isin(treasury_codes)].copy()
        treasury_net = float((treasury["debit"] - treasury["credit"]).sum())
        contra_net = float((contra["credit"] - contra["debit"]).sum())
        balance_rows.append(
            {
                "voucher_id": voucher_id,
                "treasury_net": treasury_net,
                "counter_net": contra_net,
                "variance": treasury_net - contra_net,
                "balanced": abs(treasury_net - contra_net) <= tolerance,
            }
        )
        types = {treasury_type.get(code, "Treasury") for code in treasury["account_code"]}
        is_transfer = treasury["account_code"].nunique() > 1
        if is_transfer:
            same_type = len(types) == 1
            transfer_rows.append(
                {
                    "voucher_id": voucher_id,
                    "date": voucher["date"].min(),
                    "month": voucher["month"].iloc[0],
                    "transfer_type": " to ".join(sorted(types)) if len(types) > 1 else f"{next(iter(types))} to {next(iter(types))}",
                    "amount": float(treasury["debit"].sum()),
                    "matched": bool(match_internal_transfers and same_type),
                    "voucher_detail": next((value for value in voucher["voucher_detail"] if value), ""),
                }
            )
            if match_internal_transfers and same_type and contra.empty:
                continue

        if contra.empty:
            continue
        line_rows: list[dict[str, Any]] = []
        for _, line in contra.iterrows():
            account_code = line["account_code"]
            category = _clean_text(category_by_code.get(account_code))
            if not category:
                category = line["account_name"] or "Unmapped account"
                review_rows.append(
                    {
                        "voucher_id": voucher_id,
                        "date": line["date"],
                        "month": line["month"],
                        "account_code": account_code,
                        "account_name": line["account_name"],
                        "voucher_detail": line["voucher_detail"],
                        "debit": line["debit"],
                        "credit": line["credit"],
                        "fallback_category": category,
                    }
                )
            line_rows.append(
                {
                    "category": category,
                    "month": line["month"],
                    "amount": float(line["credit"] - line["debit"]),
                    "account_code": account_code,
                    "account_name": line["account_name"],
                    "date": line["date"],
                }
            )
        customer_lines = [row for row in line_rows if is_customer_category(row["category"])]
        deduction_lines = [row for row in line_rows if is_deduction_category(row["category"])]
        if presentation == "net" and customer_lines and deduction_lines:
            customer_total = sum(row["amount"] for row in customer_lines)
            deduction_total = sum(row["amount"] for row in deduction_lines)
            first_customer = customer_lines[0]
            category_rows.append({"category": first_customer["category"], "month": first_customer["month"], "amount": customer_total + deduction_total})
            for row in deduction_lines:
                deduction_rows.append({**row, "presentation": "netted_from_customer_receipt"})
            for row in line_rows:
                if row not in customer_lines and row not in deduction_lines:
                    category_rows.append({"category": row["category"], "month": row["month"], "amount": row["amount"]})
        else:
            category_rows.extend({"category": row["category"], "month": row["month"], "amount": row["amount"]} for row in line_rows)
            if deduction_lines:
                deduction_rows.extend({**row, "presentation": "shown_separately"} for row in deduction_lines)

    balance_checks = pd.DataFrame(balance_rows)
    unbalanced = balance_checks.loc[~balance_checks["balanced"]] if not balance_checks.empty else balance_checks
    if not unbalanced.empty:
        examples = unbalanced[["voucher_id", "variance"]].head(5).to_dict("records")
        raise LedgerBalanceError(f"Cash/Bank selection contains {len(unbalanced)} unbalanced voucher(s); examples: {examples}", balance_checks)

    category_frame = pd.DataFrame(category_rows, columns=["category", "month", "amount"])
    if category_frame.empty:
        category_totals = pd.DataFrame(index=pd.Index([], name="category"), columns=[*months, "Total"], dtype=float)
    else:
        pivot = category_frame.pivot_table(index="category", columns="month", values="amount", aggfunc="sum", fill_value=0.0)
        category_totals = _reindex_months(pivot.sort_index(), months, "category")
        category_totals["Total"] = category_totals.sum(axis=1)
    review = pd.DataFrame(review_rows, columns=["voucher_id", "date", "month", "account_code", "account_name", "voucher_detail", "debit", "credit", "fallback_category"])
    deductions = pd.DataFrame(deduction_rows, columns=["category", "month", "amount", "account_code", "account_name", "date", "presentation"])
    transfers = pd.DataFrame(transfer_rows, columns=["voucher_id", "date", "month", "transfer_type", "amount", "matched", "voucher_detail"])
    return {
        "months": months,
        "category_totals": category_totals,
        "transfer_details": transfers,
        "deduction_details": deductions,
        "categories_needing_review": review,
        "balance_checks": balance_checks,
        "selected_vouchers": selected_vouchers,
        "selected_voucher_count": len(selected_vouchers),
        "source_line_count": len(working),
        "internal_transfer_count": len(transfers),
        "category_review_count": len(review),
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
