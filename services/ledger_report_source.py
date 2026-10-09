"""Application data adapter for the ledger reconciliation engine.

This module translates stored Client Data Bank rows and report-scoped Trial
Balance account assignments into the canonical DataFrames consumed by
``services.ledger_group_summary``.  It has no report calculations and never
modifies application data.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
import hashlib
import json
import re
from time import perf_counter
from typing import Any, Callable, Mapping, Sequence

import pandas as pd

from database import execute_query_pg, execute_write_pg


JOURNAL_COLUMNS = (
    "voucher_id",
    "date",
    "account_code",
    "account_name",
    "voucher_detail",
    "debit",
    "credit",
)
CASHFLOW_PREPARED_PERIOD_COLUMNS = ("transaction_month", "transaction_fortnight")
COA_COLUMNS = ("account_code", "account_title", "account_type", "category")
CASHFLOW_CHILD_DETAILS_VERSION = 7
CASHFLOW_DRILLDOWN_DETAILS_VERSION = 2
CASHFLOW_DRILLDOWN_PROJECTION_VERSION = 1
CASHFLOW_TREASURY_TOTALS_VERSION = 1
CASHFLOW_SUMMARY_PAYLOAD_VERSION = 3
CASHFLOW_TREASURY_VIEWS = ("combined", "bank", "cash", "split")
CASHFLOW_CANONICAL_SOURCE_KEY = "canonical-v1"

_JOURNAL_ALIASES: Mapping[str, tuple[str, ...]] = {
    "voucher_id": (
        "voucher_id",
        "voucher id",
        "voucher",
        "transaction id",
        "transaction ref",
        "reference no",
    ),
    "date": ("date", "transaction date", "voucher date", "posting date"),
    "account_code": (
        "account_code",
        "account code",
        "account #",
        "account no",
        "account number",
        "account detail",
        "ledger code",
    ),
    "account_name": (
        "account_name",
        "account name",
        "account title",
        "ledger name",
        "particulars",
    ),
    "voucher_detail": (
        "voucher_detail",
        "voucher detail",
        "detail",
        "description",
        "narration",
        "remarks",
    ),
    "debit": ("debit", "debit amount", "dr", "amount debit"),
    "credit": ("credit", "credit amount", "cr", "amount credit"),
}
CASHFLOW_COLUMN_ROLE_LABELS = {
    "voucher_id": "Voucher ID",
    "date": "Transaction Date",
    "account_code": "Account Detail / Account Code",
    "account_name": "Account Name",
    "voucher_detail": "Voucher Detail",
    "debit": "Debit Amount",
    "credit": "Credit Amount",
}
CASHFLOW_REQUIRED_COLUMN_ROLES = ("voucher_id", "date", "account_code", "debit", "credit")
_COA_ALIASES: Mapping[str, tuple[str, ...]] = {
    "account_code": ("account_code", "account code", "account #", "account no", "code"),
    "account_title": ("account_title", "account title", "account name", "title", "name"),
    "account_type": ("account_type", "account type", "source account type", "type"),
    "category": ("category", "category name", "cat", "assigned category"),
}


def _normalise_label(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").strip().casefold())


_JOURNAL_HEADER_LABELS = {
    _normalised
    for aliases in _JOURNAL_ALIASES.values()
    for _normalised in (_normalise_label(alias) for alias in aliases)
}


def _canonical_code(value: Any) -> str:
    if value is None or pd.isna(value):
        return ""
    if isinstance(value, (datetime, date, pd.Timestamp)):
        return pd.Timestamp(value).strftime("%m-%d-%Y")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        numeric = float(value)
        if 20000 <= numeric <= 60000:
            parsed = pd.Timestamp("1899-12-30") + pd.to_timedelta(numeric, unit="D")
            return parsed.strftime("%m-%d-%Y")
        if numeric.is_integer():
            return str(int(numeric))
    text = str(value).strip()
    text = text.replace("–", "-").replace("—", "-").replace("−", "-")
    return re.sub(r"\s+", " ", text)


def _text(value: Any) -> str:
    if value is None or pd.isna(value):
        return ""
    return str(value).strip()


def _scalar_cache_key(value: Any) -> tuple[str, str]:
    """Return a stable cache key for repeated spreadsheet scalar values."""

    if value is None:
        return ("none", "")
    if isinstance(value, float) and pd.isna(value):
        return ("nan", "")
    return (f"{type(value).__module__}.{type(value).__qualname__}", str(value))


def _parse_source_date(value: Any) -> pd.Timestamp | pd.NaT:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return pd.NaT
    if isinstance(value, (datetime, date, pd.Timestamp)):
        return pd.Timestamp(value).normalize()
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        numeric = float(value)
        if 20000 <= numeric <= 60000:
            return (pd.Timestamp("1899-12-30") + pd.to_timedelta(numeric, unit="D")).normalize()
    text = _text(value)
    if not text:
        return pd.NaT
    if re.fullmatch(r"\d+(?:\.0+)?", text):
        numeric = float(text)
        if 20000 <= numeric <= 60000:
            return (pd.Timestamp("1899-12-30") + pd.to_timedelta(numeric, unit="D")).normalize()
    for fmt in (
        "%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d", "%Y%m%d",
        "%d-%m-%Y", "%d/%m/%Y", "%d.%m.%Y", "%d-%m-%y", "%d/%m/%y",
        "%d-%b-%Y", "%d/%b/%Y", "%d-%b-%y", "%d/%b/%y",
    ):
        try:
            return pd.to_datetime(text, format=fmt, errors="raise").normalize()
        except (TypeError, ValueError):
            continue
    parsed = pd.to_datetime(text, errors="coerce", dayfirst=True)
    return parsed.normalize() if not pd.isna(parsed) else pd.NaT


def _parse_amount(value: Any) -> tuple[float, bool]:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return 0.0, False
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value), False
    text = _text(value)
    if not text or text.casefold() in {"-", "—", "–", "n/a", "na"}:
        return 0.0, False
    negative = text.startswith("(") and text.endswith(")")
    if negative:
        text = text[1:-1]
    if text.endswith("-"):
        negative = True
        text = text[:-1]
    text = re.sub(r"[^0-9.\-]", "", text.replace(",", ""))
    if not text or text in {".", "-", "-."}:
        return 0.0, True
    try:
        amount = float(text)
    except ValueError:
        return 0.0, True
    return (-amount if negative else amount), False


def _is_layout_row(row: Mapping[str, Any], resolved: Mapping[str, Any]) -> tuple[bool, str]:
    values = set()
    for value in row.values():
        text = _text(value)
        if text:
            values.add(text)
    if not values:
        return True, "empty_row"
    normalised = {_normalise_label(value) for value in values}
    if len(normalised & _JOURNAL_HEADER_LABELS) >= 2:
        return True, "repeated_header"
    account_name = _text(row.get(resolved.get("account_name")))
    account_code = _text(row.get(resolved.get("account_code")))
    debit, _ = _parse_amount(row.get(resolved.get("debit")))
    credit, _ = _parse_amount(row.get(resolved.get("credit")))
    label = account_name or account_code or next(iter(values), "")
    if re.match(r"^(total|sub[ -]?total|grand total|opening|closing|carried forward|b/f|c/f)\b", label.casefold()):
        return True, "layout_total_or_balance"
    if not account_code and debit == 0 and credit == 0:
        return True, "blank_layout_row"
    return False, ""


def _reject_record(row: Mapping[str, Any], row_number: int, reason: str) -> dict[str, Any]:
    return {
        "source_row_number": row_number,
        "reason": reason,
        "voucher_id": _text(row.get("voucher_id")),
        "date": _text(row.get("date")),
        "account_code": _canonical_code(row.get("account_code")),
        "account_name": _text(row.get("account_name")),
        "debit": float(row.get("debit") or 0),
        "credit": float(row.get("credit") or 0),
    }


def _diagnostic(code: str, severity: str, message: str, **extra: Any) -> dict[str, Any]:
    result = {"code": code, "severity": severity, "message": message}
    result.update(extra)
    return result


def _empty_journal() -> pd.DataFrame:
    return pd.DataFrame(columns=list(JOURNAL_COLUMNS))


def _empty_coa() -> pd.DataFrame:
    return pd.DataFrame(columns=list(COA_COLUMNS))


def _decode_row_data(value: Any) -> dict[str, Any] | None:
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except (TypeError, ValueError):
            return None
        return dict(decoded) if isinstance(decoded, Mapping) else None
    return None


def _resolve_columns(
    columns: Sequence[Any],
    aliases: Mapping[str, Sequence[str]],
    diagnostics: list[dict[str, Any]],
    preferred: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    by_label: dict[str, list[Any]] = {}
    for column in columns:
        by_label.setdefault(_normalise_label(column), []).append(column)

    resolved: dict[str, Any] = {}
    for canonical, source_name in (preferred or {}).items():
        if canonical not in aliases:
            continue
        matches = by_label.get(_normalise_label(source_name), [])
        if not matches:
            diagnostics.append(
                _diagnostic(
                    "mapped_source_column_missing",
                    "error",
                    f"Configured source column '{source_name}' for '{canonical}' was not found.",
                    field=canonical,
                    source_column=str(source_name),
                )
            )
            continue
        resolved[canonical] = matches[0]

    for canonical, candidates in aliases.items():
        if canonical in resolved:
            continue
        for candidate in candidates:
            matches = by_label.get(_normalise_label(candidate), [])
            if matches:
                resolved[canonical] = matches[0]
                if len(matches) > 1:
                    diagnostics.append(
                        _diagnostic(
                            "ambiguous_source_column",
                            "warning",
                            f"Multiple source columns match '{canonical}'; '{matches[0]}' was selected.",
                            field=canonical,
                            columns=[str(item) for item in matches],
                        )
                    )
                break
    return resolved


def _normalise_account_rows(
    account_rows: Sequence[Mapping[str, Any]] | pd.DataFrame | None,
    diagnostics: list[dict[str, Any]],
) -> pd.DataFrame:
    if account_rows is None:
        return _empty_coa()
    raw = account_rows.to_dict("records") if isinstance(account_rows, pd.DataFrame) else list(account_rows)
    if not raw:
        diagnostics.append(
            _diagnostic(
                "no_chart_of_accounts",
                "warning",
                "No account-category rows were found; contra lines will require review.",
            )
        )
        return _empty_coa()

    frame = pd.DataFrame(raw)
    resolved = _resolve_columns(frame.columns, _COA_ALIASES, diagnostics)
    if "account_code" not in resolved:
        diagnostics.append(
            _diagnostic(
                "missing_account_code_column",
                "error",
                "The stored Chart of Accounts has no account-code column.",
                field="account_code",
            )
        )
        return _empty_coa()

    result = pd.DataFrame(index=frame.index)
    for canonical in COA_COLUMNS:
        source = resolved.get(canonical)
        result[canonical] = frame[source] if source is not None else ""
    result["account_code"] = result["account_code"].map(_canonical_code)
    for column in ("account_title", "account_type", "category"):
        result[column] = result[column].map(_text)
    result = result.loc[result["account_code"].ne("")].copy()

    duplicate_count = int(result["account_code"].duplicated(keep=False).sum())
    if duplicate_count:
        diagnostics.append(
            _diagnostic(
                "duplicate_account_codes",
                "warning",
                "Duplicate account codes were found; the last row is used for category lookup.",
                count=duplicate_count,
            )
        )
        result = result.drop_duplicates("account_code", keep="last")
    return result.reset_index(drop=True)


@dataclass(frozen=True)
class LedgerReportSource:
    """Canonical report inputs plus diagnostics and quarantined source rows."""

    journal: pd.DataFrame
    coa: pd.DataFrame
    diagnostics: tuple[dict[str, Any], ...]
    metadata: Mapping[str, Any]
    rejected_rows: tuple[dict[str, Any], ...] = ()

    @property
    def ready(self) -> bool:
        return not any(item.get("severity") == "error" for item in self.diagnostics)

    def blocking_diagnostics(self) -> list[dict[str, Any]]:
        return [item for item in self.diagnostics if item.get("severity") == "error"]


@dataclass(frozen=True)
class CashflowVoucherOccurrenceSource:
    """Complete normalized voucher lines for canonical-occurrence maintenance.

    Unlike a report source, this intentionally retains every upload of the
    configured Cashflows template. It is an audit/preparation input only and
    never applies filename-based source selection or report-date filtering.
    """

    template_id: int | None
    rows: tuple[dict[str, Any], ...]
    diagnostics: tuple[dict[str, Any], ...]
    metadata: Mapping[str, Any]

    @property
    def ready(self) -> bool:
        return self.template_id is not None and not any(
            item.get("severity") == "error" for item in self.diagnostics
        )


@dataclass(frozen=True)
class CashflowCanonicalVoucherSelection:
    """Indexed current voucher occurrences available to the Cashflows fast path.

    The durable Cashflows report revision controls cache freshness. The stable
    source key intentionally does not change for an exact reupload, because
    the accounting population is unchanged in that case.
    """

    template_id: int
    occurrence_count: int
    upload_ids: tuple[int, ...]

    @property
    def ready(self) -> bool:
        return self.occurrence_count > 0

    @property
    def source_key(self) -> str:
        return CASHFLOW_CANONICAL_SOURCE_KEY


@dataclass(frozen=True)
class CashflowReportSourceIdentity:
    """The stable report population identity used by every Cashflows cache."""

    template_id: int
    source_key: str
    selection_mode: str
    upload_ids: tuple[int, ...]
    duplicate_uploads_suppressed: int
    canonical_occurrence_count: int = 0

    @property
    def ready(self) -> bool:
        return bool(self.upload_ids)


def build_report_source(
    client_data_rows: Sequence[Mapping[str, Any]] | pd.DataFrame,
    account_rows: Sequence[Mapping[str, Any]] | pd.DataFrame | None = None,
    *,
    period_start: Any = None,
    period_end: Any = None,
    column_mapping: Mapping[str, Any] | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> LedgerReportSource:
    """Normalize one Client Data Bank dataset for generic Cashflows reporting.

    Repairable layout and value problems are quarantined as warnings. Only an
    unusable schema, empty source, or invalid reporting period blocks a report.
    """

    diagnostics: list[dict[str, Any]] = []
    source_metadata = dict(metadata or {})
    raw_rows = client_data_rows.to_dict("records") if isinstance(client_data_rows, pd.DataFrame) else list(client_data_rows)
    records: list[dict[str, Any]] = []
    rejected_rows: list[dict[str, Any]] = []
    invalid_json_count = 0
    for source_row_number, row in enumerate(raw_rows, start=2):
        source = row.get("row_data") if isinstance(row, Mapping) and "row_data" in row else row
        decoded = _decode_row_data(source)
        if decoded is None:
            invalid_json_count += 1
            rejected_rows.append({"source_row_number": source_row_number, "reason": "invalid_json"})
            continue
        decoded["__source_row_number"] = source_row_number
        records.append(decoded)
    if invalid_json_count:
        diagnostics.append(
            _diagnostic(
                "invalid_client_data_row",
                "warning",
                f"{invalid_json_count} stored Client Data row(s) were quarantined because they do not contain a JSON object.",
                count=invalid_json_count,
            )
        )
    if not records:
        diagnostics.append(_diagnostic("no_journal_rows", "error", "No usable Client Data journal rows were found."))
        return LedgerReportSource(
            _empty_journal(),
            _normalise_account_rows(account_rows, diagnostics),
            tuple(diagnostics),
            source_metadata,
            tuple(rejected_rows),
        )

    frame = pd.DataFrame(records)
    resolved = _resolve_columns(frame.columns, _JOURNAL_ALIASES, diagnostics, preferred=column_mapping)
    missing = [column for column in ("voucher_id", "date", "account_code", "debit", "credit") if column not in resolved]
    if missing:
        diagnostics.append(
            _diagnostic(
                "missing_journal_columns",
                "error",
                "The stored journal is missing required field(s): " + ", ".join(missing) + ".",
                fields=missing,
            )
        )
        return LedgerReportSource(
            _empty_journal(),
            _normalise_account_rows(account_rows, diagnostics),
            tuple(diagnostics),
            source_metadata,
            tuple(rejected_rows),
        )

    layout_count = 0
    usable_records: list[dict[str, Any]] = []
    for record in records:
        is_layout, reason = _is_layout_row(record, resolved)
        if is_layout:
            layout_count += 1
            rejected_rows.append({
                "source_row_number": record.get("__source_row_number"),
                "reason": reason,
                "account_name": _text(record.get(resolved.get("account_name"))),
                "account_code": _canonical_code(record.get(resolved.get("account_code"))),
            })
        else:
            usable_records.append(record)
    if layout_count:
        diagnostics.append(
            _diagnostic(
                "journal_layout_rows_removed",
                "warning",
                f"{layout_count} blank, repeated-header, total, or balance layout row(s) were removed from the journal.",
                count=layout_count,
            )
        )

    clean_frame = pd.DataFrame(usable_records).reindex(columns=frame.columns)
    if clean_frame.empty:
        source_metadata.update({"layout_row_count": layout_count, "rejected_row_count": len(rejected_rows), "valid_row_count": 0})
        diagnostics.append(_diagnostic("no_journal_rows", "error", "No transaction rows remained after removing layout rows."))
        return LedgerReportSource(
            _empty_journal(),
            _normalise_account_rows(account_rows, diagnostics),
            tuple(diagnostics),
            source_metadata,
            tuple(rejected_rows),
        )

    # Continuation lines inherit the voucher/date immediately above them; the
    # original order is retained until after this repair is complete. Applying
    # this to the decoded records avoids repeated pandas scalar writes across
    # large monthwise template datasets.
    for source_column in (resolved["voucher_id"], resolved["date"]):
        previous = None
        for record in usable_records:
            value = record.get(source_column)
            if _text(value):
                previous = value
            elif previous is not None:
                record[source_column] = previous

    clean_frame = pd.DataFrame(usable_records).reindex(columns=frame.columns)

    journal = pd.DataFrame(index=clean_frame.index)
    for canonical in JOURNAL_COLUMNS:
        source = resolved.get(canonical)
        journal[canonical] = clean_frame[source] if source is not None else ""
    journal["voucher_id"] = journal["voucher_id"].map(_text)
    journal["account_code"] = journal["account_code"].map(_canonical_code)
    for column in ("account_name", "voucher_detail"):
        journal[column] = journal[column].map(_text)

    date_cache: dict[tuple[str, str], pd.Timestamp | pd.NaT] = {}
    amount_cache: dict[tuple[str, str], tuple[float, bool]] = {}

    def parse_date_cached(value: Any) -> pd.Timestamp | pd.NaT:
        key = _scalar_cache_key(value)
        if key not in date_cache:
            date_cache[key] = _parse_source_date(value)
        return date_cache[key]

    def parse_amount_cached(value: Any) -> tuple[float, bool]:
        key = _scalar_cache_key(value)
        if key not in amount_cache:
            amount_cache[key] = _parse_amount(value)
        return amount_cache[key]

    parsed_dates = journal["date"].map(parse_date_cached)
    journal["date"] = parsed_dates
    parsed_amounts: dict[str, list[float]] = {"debit": [], "credit": []}
    invalid_amounts: dict[str, set[int]] = {"debit": set(), "credit": set()}
    for row_index in journal.index:
        for column in ("debit", "credit"):
            amount, invalid = parse_amount_cached(journal.at[row_index, column])
            parsed_amounts[column].append(amount)
            if invalid:
                invalid_amounts[column].add(row_index)
    for column in ("debit", "credit"):
        journal[column] = parsed_amounts[column]
        if invalid_amounts[column]:
            diagnostics.append(
                _diagnostic(
                    "invalid_journal_amounts",
                    "warning",
                    f"{len(invalid_amounts[column])} journal row(s) with non-numeric {column} values were quarantined.",
                    field=column,
                    count=len(invalid_amounts[column]),
                )
            )

    valid_indexes: list[Any] = []
    for row_index in journal.index:
        reasons: list[str] = []
        if not journal.at[row_index, "voucher_id"]:
            reasons.append("blank_voucher_id")
        if pd.isna(journal.at[row_index, "date"]):
            reasons.append("invalid_or_blank_date")
        if not journal.at[row_index, "account_code"]:
            reasons.append("blank_account_code")
        if row_index in invalid_amounts["debit"] or row_index in invalid_amounts["credit"]:
            reasons.append("invalid_amount")
        if float(journal.at[row_index, "debit"]) == 0 and float(journal.at[row_index, "credit"]) == 0:
            reasons.append("zero_amount")
        if reasons:
            row = journal.loc[row_index].to_dict()
            rejected_rows.append(_reject_record(row, int(clean_frame.at[row_index, "__source_row_number"]), ";".join(reasons)))
        else:
            valid_indexes.append(row_index)
    journal = journal.loc[valid_indexes].copy()

    if rejected_rows:
        reason_counts: dict[str, int] = {}
        for item in rejected_rows:
            reason = str(item.get("reason") or "rejected")
            reason_counts[reason] = reason_counts.get(reason, 0) + 1
        source_metadata.update({
            "rejected_row_count": len(rejected_rows),
            "rejected_reason_counts": reason_counts,
            "rejected_rows_sample": rejected_rows[:100],
        })
        diagnostics.append(
            _diagnostic(
                "rejected_journal_rows",
                "warning",
                f"{len(rejected_rows)} journal row(s) were quarantined; the report uses the remaining valid rows.",
                count=len(rejected_rows),
                reason_counts=reason_counts,
            )
        )
    else:
        source_metadata["rejected_row_count"] = 0
    source_metadata["layout_row_count"] = layout_count
    source_metadata["valid_row_count"] = len(journal)

    if period_start is not None or period_end is not None:
        start = pd.Timestamp(period_start) if period_start is not None else journal["date"].min()
        end = pd.Timestamp(period_end) if period_end is not None else journal["date"].max()
        if pd.isna(start) or pd.isna(end) or start > end:
            diagnostics.append(_diagnostic("invalid_reporting_period", "error", "The reporting period is invalid."))
        else:
            before_count = len(journal)
            journal = journal.loc[journal["date"].between(start, end, inclusive="both")].copy()
            source_metadata["out_of_period_row_count"] = before_count - len(journal)

    coa = _normalise_account_rows(account_rows, diagnostics)
    coa_codes = set(coa["account_code"].tolist())
    missing_coa_codes = sorted(set(journal["account_code"].dropna()) - coa_codes)
    if missing_coa_codes:
        diagnostics.append(
            _diagnostic(
                "journal_accounts_missing_from_coa",
                "warning",
                f"{len(missing_coa_codes)} journal account code(s) are absent from the Chart of Accounts.",
                account_codes=missing_coa_codes[:20],
                count=len(missing_coa_codes),
            )
        )

    return LedgerReportSource(
        journal.reset_index(drop=True),
        coa,
        tuple(diagnostics),
        source_metadata,
        tuple(rejected_rows),
    )

def _load_account_rows(
    client_id: int,
    query_fn: Callable[..., list[dict[str, Any]]],
    diagnostics: list[dict[str, Any]],
    *,
    category_source: str = "ledger_summary",
) -> list[dict[str, Any]]:
    latest_rows = query_fn(
        """
        SELECT a.account_code, a.account_name, a.source_account_type
        FROM trial_balance_accounts a
        JOIN trial_balance_uploads u ON u.id = a.upload_id
        WHERE u.client_id = %s AND u.status = 'parsed'
          AND u.id = (
              SELECT id FROM trial_balance_uploads
              WHERE client_id = %s AND status = 'parsed'
              ORDER BY (period_end IS NULL), period_end DESC, uploaded_at DESC
              LIMIT 1
          )
        ORDER BY a.sort_order
        """,
        (client_id, client_id),
    )
    assignment_rows = []
    if category_source == "financial_statement":
        assignment_rows = query_fn(
            """
            SELECT a.account_code, c.label AS category_name
            FROM account_control_assignments a
            JOIN account_control_categories c
              ON c.id = a.financial_statement_category_id
             AND c.client_id = a.client_id
             AND c.category_type = 'financial_statement'
             AND c.is_active = TRUE
            WHERE a.client_id = %s
              AND a.financial_statement_category_id IS NOT NULL
            """,
            (client_id,),
        )
        if not assignment_rows:
            diagnostics.append(
                _diagnostic(
                    "no_financial_statement_mappings",
                    "warning",
                    "No active Financial Statement account mappings are configured; unmapped accounts will require review.",
                )
            )
    elif category_source == "cashflow":
        assignment_rows = query_fn(
            """
            SELECT a.account_code, c.label AS category_name
            FROM account_control_cashflow_category_assignments a
            JOIN account_control_cashflow_categories c
              ON c.id = a.category_id AND c.client_id = a.client_id AND c.is_active = TRUE
            WHERE a.client_id = %s AND a.category_id IS NOT NULL
            """,
            (client_id,),
        )
        if not assignment_rows:
            diagnostics.append(
                _diagnostic(
                    "no_cashflow_category_mappings",
                    "warning",
                    "No active Cash Flow account mappings are configured; counter-account lines will require review.",
                )
            )
    else:
        category_set_rows = query_fn(
            """
            SELECT id
            FROM report_category_sets
            WHERE client_id = %s AND purpose_key = 'ledger_summary' AND is_active = TRUE
            ORDER BY id DESC
            LIMIT 1
            """,
            (client_id,),
        )
        if category_set_rows:
            assignment_rows = query_fn(
                """
                SELECT a.account_code, c.label AS category_name
                FROM report_account_category_assignments a
                JOIN report_category_sets s ON s.id = a.category_set_id
                JOIN report_categories c ON c.id = a.category_id AND c.is_active = TRUE
                WHERE a.client_id = %s AND a.category_set_id = %s
                  AND s.purpose_key = 'ledger_summary' AND s.is_active = TRUE
                """,
                (client_id, category_set_rows[0]["id"]),
            )
        else:
            diagnostics.append(
                _diagnostic(
                    "no_ledger_summary_category_set",
                    "warning",
                    "No active Ledger Summary category set is configured; unmapped accounts will require review.",
                )
            )

    merged: dict[str, dict[str, Any]] = {}
    for row in latest_rows:
        code = _canonical_code(row.get("account_code"))
        if code:
            merged[code] = {
                "account_code": code,
                "account_title": row.get("account_name"),
                "account_type": row.get("source_account_type"),
                "category": "",
            }
    for row in assignment_rows:
        code = _canonical_code(row.get("account_code"))
        if not code:
            continue
        target = merged.setdefault(
            code,
            {"account_code": code, "account_title": "", "account_type": "", "category": ""},
        )
        if not target.get("category") and row.get("category_name"):
            target["category"] = row["category_name"]
    return list(merged.values())


def _load_cashflow_column_mapping(
    client_id: int,
    template_id: int,
    query_fn: Callable[..., list[dict[str, Any]]],
) -> dict[str, str]:
    rows = query_fn(
        """
        SELECT m.role, c.column_name
        FROM account_control_cashflow_source_mappings m
        JOIN master_columns c ON c.id = m.template_column_id
        WHERE m.client_id = %s AND m.template_id = %s
        ORDER BY m.role
        """,
        (client_id, template_id),
    )
    return {str(row["role"]): str(row["column_name"]) for row in rows if row.get("role") and row.get("column_name")}


def latest_template_upload_selection_cte() -> str:
    """Return the canonical latest-upload-per-filename selection CTE."""

    return """
        WITH latest_template_uploads AS (
            SELECT
                f.id AS upload_id,
                f.file_name,
                f.upload_date,
                ROW_NUMBER() OVER (
                    PARTITION BY COALESCE(NULLIF(BTRIM(f.file_name), ''), f.id::text)
                    ORDER BY f.upload_date DESC NULLS LAST, f.id DESC
                ) AS upload_rank
            FROM uploaded_files f
            WHERE f.client_id = %s
              AND EXISTS (
                  SELECT 1
                  FROM client_data cd
                  WHERE cd.upload_id = f.id
                    AND cd.client_id = %s
                    AND cd.template_id = %s
                    AND cd.is_deleted = FALSE
              )
        ),
        selected_template_uploads AS (
            SELECT upload_id
            FROM latest_template_uploads
            WHERE upload_rank = 1
        )
    """


def select_latest_template_upload_ids(
    client_id: int,
    template_id: int,
    query_fn: Callable[..., list[dict[str, Any]]],
) -> tuple[list[int], int]:
    """Select the newest upload for each filename without reading row_data."""

    rows = query_fn(
        f"""
        {latest_template_upload_selection_cte()}
        SELECT upload_id, file_name, upload_date
        FROM latest_template_uploads
        ORDER BY upload_date DESC NULLS LAST, upload_id DESC
        """,
        (client_id, client_id, template_id),
    )
    latest_by_name: dict[str, int] = {}
    for row in rows:
        upload_id = row.get("upload_id")
        if upload_id is None:
            continue
        file_name = str(row.get("file_name") or "").strip() or f"__upload_{upload_id}"
        latest_by_name.setdefault(file_name, int(upload_id))
    selected = sorted(latest_by_name.values())
    return selected, max(0, len(rows) - len(selected))


def canonical_cashflow_voucher_selection_cte() -> str:
    """Return the report selector for one indexed canonical voucher version.

    Raw uploads remain untouched. A report path joining this CTE must use the
    selected upload and normalized voucher identity together, so an overlapping
    monthly/daily upload cannot contribute the same voucher twice.
    """

    return """
        WITH selected_canonical_vouchers AS (
            SELECT
                occurrence.id AS canonical_occurrence_id,
                occurrence.upload_id,
                occurrence.voucher_identity,
                occurrence.voucher_fingerprint
            FROM cashflow_voucher_occurrences occurrence
            WHERE occurrence.client_id = %s
              AND occurrence.template_id = %s
              AND occurrence.canonical_occurrence_id = occurrence.id
              AND occurrence.decision_status IN ('current', 'current_revised')
        )
    """


def canonical_cashflow_report_voucher_selection_cte() -> str:
    """Return the active report selector backed by the canonical register.

    Non-blank vouchers use the self-linked current occurrence. Blank Voucher IDs
    remain visible per upload as review-required rows, matching the fresh-start
    selector's safety rule.
    """

    return """
        WITH selected_canonical_vouchers AS (
            SELECT
                occurrence.id AS canonical_occurrence_id,
                occurrence.upload_id,
                occurrence.voucher_identity,
                occurrence.voucher_fingerprint
            FROM cashflow_voucher_occurrences occurrence
            WHERE occurrence.client_id = %s
              AND occurrence.template_id = %s
              AND occurrence.canonical_occurrence_id = occurrence.id
              AND occurrence.decision_status IN ('current', 'current_revised')
              AND occurrence.voucher_identity IS NOT NULL
        ), selected_blank_vouchers AS (
            SELECT DISTINCT occurrence.upload_id, ''::text AS voucher_identity
            FROM cashflow_voucher_occurrences occurrence
            WHERE occurrence.client_id = %s
              AND occurrence.template_id = %s
              AND occurrence.voucher_identity IS NULL
              AND occurrence.decision_status = 'review_required'
        ), selected_report_vouchers AS (
            SELECT upload_id, voucher_identity FROM selected_canonical_vouchers
            UNION ALL
            SELECT upload_id, voucher_identity FROM selected_blank_vouchers
        )
    """

def select_canonical_cashflow_vouchers(
    client_id: int,
    template_id: int,
    query_fn: Callable[..., list[dict[str, Any]]],
) -> CashflowCanonicalVoucherSelection:
    """Load the small indexed selector needed by the canonical report path."""

    rows = query_fn(
        f"""
        {canonical_cashflow_voucher_selection_cte()}
        SELECT canonical_occurrence_id, upload_id
        FROM selected_canonical_vouchers
        ORDER BY canonical_occurrence_id
        """,
        (client_id, template_id),
    )
    occurrence_ids = {
        int(row["canonical_occurrence_id"])
        for row in rows
        if row.get("canonical_occurrence_id") is not None
    }
    upload_ids = {
        int(row["upload_id"])
        for row in rows
        if row.get("upload_id") is not None
    }
    return CashflowCanonicalVoucherSelection(
        template_id=int(template_id),
        occurrence_count=len(occurrence_ids),
        upload_ids=tuple(sorted(upload_ids)),
    )


def _cashflow_source_identity_from_rows(
    rows: list[dict[str, Any]],
    *,
    template_id: int,
    selection_mode: str,
    source_prefix: str,
) -> CashflowReportSourceIdentity:
    upload_ids = tuple(sorted({int(row["upload_id"]) for row in rows if row.get("upload_id") is not None}))
    population_parts = [
        f"{int(row['upload_id'])}:{str(row.get('voucher_identity') or '')}"
        for row in rows
        if row.get("upload_id") is not None
    ]
    population_key = hashlib.sha256("\n".join(population_parts).encode("utf-8")).hexdigest()[:20]
    suppressed_count = int(rows[0].get("duplicate_uploads_suppressed") or 0) if rows else 0
    return CashflowReportSourceIdentity(
        template_id=int(template_id),
        source_key=f"{source_prefix}:{population_key}" if rows else "",
        selection_mode=selection_mode,
        upload_ids=upload_ids,
        duplicate_uploads_suppressed=suppressed_count,
    )


def select_cashflow_report_source_identity(
    client_id: int,
    template_id: int,
    query_fn: Callable[..., list[dict[str, Any]]],
) -> CashflowReportSourceIdentity:
    """Resolve the report population from the indexed register, with safe fallback."""

    canonical_rows = query_fn(
        f"""
        {canonical_cashflow_report_voucher_selection_cte()}
        , register_stats AS (
            SELECT
                (
                    SELECT COUNT(*)
                    FROM cashflow_voucher_occurrences occurrence
                    WHERE occurrence.client_id = %s
                      AND occurrence.template_id = %s
                      AND occurrence.voucher_identity IS NOT NULL
                ) - (
                    SELECT COUNT(*) FROM selected_canonical_vouchers
                ) AS duplicate_uploads_suppressed
        )
        SELECT canonical_occurrence_id, upload_id, voucher_identity,
               register_stats.duplicate_uploads_suppressed
        FROM (
            SELECT canonical_occurrence_id, upload_id, voucher_identity
            FROM selected_canonical_vouchers
            UNION ALL
            SELECT NULL::bigint, upload_id, voucher_identity
            FROM selected_blank_vouchers
        ) selected_register
        CROSS JOIN register_stats
        ORDER BY upload_id, voucher_identity
        """,
        (client_id, template_id, client_id, template_id, client_id, template_id),
    )
    if canonical_rows:
        return _cashflow_source_identity_from_rows(
            canonical_rows,
            template_id=template_id,
            selection_mode="canonical_register",
            source_prefix="canonical-v1",
        )

    rows = query_fn(
        f"""
        {cashflow_report_voucher_selection_cte()}
        SELECT upload_id, voucher_identity,
               (
                   SELECT COUNT(*) FROM eligible_nonblank_occurrences
               ) - (
                   SELECT COUNT(*) FROM selected_nonblank_vouchers
               ) AS duplicate_uploads_suppressed
        FROM selected_report_vouchers
        ORDER BY upload_id, voucher_identity
        """,
        (client_id, template_id, client_id, client_id, template_id, client_id, template_id),
    )
    return _cashflow_source_identity_from_rows(
        rows,
        template_id=template_id,
        selection_mode="latest_voucher",
        source_prefix="latest-voucher-v1",
    )

def cashflow_report_voucher_selection_cte() -> str:
    """Select one latest complete upload occurrence per normalized Voucher ID.

    This is the fresh-start Excel-style rule. It deliberately does not activate
    the parked occurrence register. Blank Voucher IDs are retained per upload so
    they remain visible without being merged into one synthetic voucher.
    """

    return """
        WITH source_request AS (
            SELECT %s::bigint AS client_id, %s::bigint AS template_id
        ), eligible_template_uploads AS (
            SELECT f.id AS upload_id, f.upload_date
            FROM uploaded_files f
            WHERE f.client_id = %s
              AND f.client_id = (SELECT client_id FROM source_request)
              AND EXISTS (
                  SELECT 1 FROM client_data cd
                  WHERE cd.upload_id = f.id
                    AND cd.client_id = %s
                    AND cd.template_id = %s
                    AND cd.client_id = (SELECT client_id FROM source_request)
                    AND cd.template_id = (SELECT template_id FROM source_request)
                    AND cd.is_deleted = FALSE
              )
        ), eligible_voucher_occurrences AS (
            SELECT DISTINCT
                c.upload_id,
                UPPER(REGEXP_REPLACE(COALESCE(c.voucher_id, ''), '\\s+', '', 'g')) AS voucher_identity,
                eligible.upload_date
            FROM cash_bank_transaction_classifications c
            JOIN eligible_template_uploads eligible
              ON eligible.upload_id = c.upload_id
            WHERE c.client_id = %s
              AND c.template_id = %s
              AND c.is_current = TRUE
        ), eligible_nonblank_occurrences AS (
            SELECT upload_id, voucher_identity, upload_date
            FROM eligible_voucher_occurrences
            WHERE voucher_identity <> ''
        ), ranked_nonblank_occurrences AS (
            SELECT upload_id, voucher_identity,
                   ROW_NUMBER() OVER (
                       PARTITION BY voucher_identity
                       ORDER BY upload_date DESC NULLS LAST, upload_id DESC
                   ) AS occurrence_rank
            FROM eligible_nonblank_occurrences
        ), selected_nonblank_vouchers AS (
            SELECT upload_id, voucher_identity
            FROM ranked_nonblank_occurrences
            WHERE occurrence_rank = 1
        ), selected_blank_vouchers AS (
            SELECT DISTINCT upload_id, voucher_identity
            FROM eligible_voucher_occurrences
            WHERE voucher_identity = ''
        ), selected_report_vouchers AS (
            SELECT upload_id, voucher_identity FROM selected_nonblank_vouchers
            UNION ALL
            SELECT upload_id, voucher_identity FROM selected_blank_vouchers
        )
    """

def load_cashflow_voucher_occurrence_source(
    client_id: int,
    *,
    query_fn: Callable[..., list[dict[str, Any]]] = execute_query_pg,
) -> CashflowVoucherOccurrenceSource:
    """Load complete normalized lines from every configured-template upload.

    This intentionally bypasses filename-based source selection: overlapping
    uploads are the evidence that canonical voucher resolution must assess.
    It is read-only and is not used by report generation.
    """
    settings_rows = query_fn(
        """
        SELECT source_template_id
        FROM account_control_cashflow_settings
        WHERE client_id = %s
        """,
        (client_id,),
    )
    template_id = settings_rows[0].get("source_template_id") if settings_rows else None
    if template_id is None:
        return CashflowVoucherOccurrenceSource(
            None,
            (),
            (_diagnostic("cashflow_template_not_configured", "error", "Configure a Cashflows source template before resolving voucher occurrences."),),
            {"client_id": client_id, "source_upload_selection": "all_configured_template_uploads"},
        )
    template_id = int(template_id)
    mapping = _load_cashflow_column_mapping(client_id, template_id, query_fn)
    missing_roles = [role for role in CASHFLOW_REQUIRED_COLUMN_ROLES if role not in mapping]
    if missing_roles:
        return CashflowVoucherOccurrenceSource(
            template_id,
            (),
            (_diagnostic("cashflow_column_mapping_incomplete", "error", "Required Cashflows template columns are not mapped: " + ", ".join(missing_roles), fields=missing_roles, template_id=template_id),),
            {"client_id": client_id, "template_id": template_id, "source_upload_selection": "all_configured_template_uploads"},
        )

    raw_rows = query_fn(
        """
        SELECT cd.id AS client_data_id, cd.client_id, cd.template_id, cd.upload_id,
               cd.row_data, f.upload_date AS source_extract_as_of
        FROM client_data cd
        LEFT JOIN uploaded_files f ON f.id = cd.upload_id
        WHERE cd.client_id = %s
          AND cd.template_id = %s
          AND cd.is_deleted = FALSE
        ORDER BY f.upload_date NULLS LAST, cd.upload_id, cd.id
        """,
        (client_id, template_id),
    )
    uploads: dict[int, list[dict[str, Any]]] = {}
    for row in raw_rows:
        upload_id = row.get("upload_id")
        if upload_id is not None:
            uploads.setdefault(int(upload_id), []).append(dict(row))

    lines: list[dict[str, Any]] = []
    diagnostics: list[dict[str, Any]] = []
    skipped_upload_ids: list[int] = []
    for upload_id, upload_rows in uploads.items():
        normalized = build_report_source(
            upload_rows,
            column_mapping=mapping,
            metadata={"client_id": client_id, "template_id": template_id, "upload_id": upload_id},
        )
        diagnostics.extend({**diagnostic, "upload_id": upload_id} for diagnostic in normalized.diagnostics)
        if not normalized.ready:
            skipped_upload_ids.append(upload_id)
            continue
        source_extract_as_of = _text(upload_rows[0].get("source_extract_as_of"))
        source_sequence: Any = source_extract_as_of or upload_id
        for row in normalized.journal.to_dict("records"):
            lines.append(
                {
                    "client_id": client_id,
                    "template_id": template_id,
                    "upload_id": upload_id,
                    "voucher_id": row.get("voucher_id"),
                    "transaction_date": row.get("date"),
                    "account_code": row.get("account_code"),
                    "account_name": row.get("account_name"),
                    "voucher_detail": row.get("voucher_detail"),
                    "debit": row.get("debit"),
                    "credit": row.get("credit"),
                    "source_extract_as_of": source_extract_as_of or None,
                    "source_sequence": source_sequence,
                }
            )
    metadata = {
        "client_id": client_id,
        "template_id": template_id,
        "source_upload_selection": "all_configured_template_uploads",
        "source_upload_count": len(uploads),
        "source_upload_ids": sorted(uploads),
        "skipped_upload_ids": skipped_upload_ids,
        "source_row_count": len(raw_rows),
        "normalized_line_count": len(lines),
    }
    return CashflowVoucherOccurrenceSource(template_id, tuple(lines), tuple(diagnostics), metadata)


def _cashflow_upload_ids_key(upload_ids: Sequence[int]) -> str:
    return ",".join(str(int(upload_id)) for upload_id in sorted(set(upload_ids)))


def _cashflow_json_scalar(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, pd.Timestamp):
        return value.date().isoformat()
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    try:
        if bool(pd.isna(value)):
            return None
    except (TypeError, ValueError):
        pass
    if hasattr(value, "item"):
        try:
            return _cashflow_json_scalar(value.item())
        except (TypeError, ValueError):
            pass
    if isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def normalize_cashflow_treasury_view(value: Any) -> str:
    view = str(value or "combined").strip().casefold()
    if view not in CASHFLOW_TREASURY_VIEWS:
        allowed = ", ".join(CASHFLOW_TREASURY_VIEWS)
        raise ValueError(f"treasury_view must be one of: {allowed}")
    return view


def load_cashflow_prepared_rollup_summary(
    client_id: int,
    *,
    journal_template_id: int | None,
    period_start: Any,
    period_end: Any,
    group_by: str,
    receipt_presentation: str,
    treasury_view: str = "combined",
    query_fn: Callable[..., list[dict[str, Any]]] = execute_query_pg,
) -> dict[str, Any] | None:
    """Read active upload-time rollups for the lightweight Cashflows summary."""
    from services.ledger_group_summary import (
        DEFAULT_TOLERANCE,
        _cashflow_periods,
        _cashflow_treasury_reconciliation,
        normalize_cashflow_grouping,
    )

    if journal_template_id is None:
        return None
    view = normalize_cashflow_treasury_view(treasury_view)
    # Split is still served by the controlled fallback until the prepared
    # model stores the full split presentation contract. Cash and Bank can be
    # projected safely from the prepared voucher lines below.
    if view == "split":
        return None
    grouping = normalize_cashflow_grouping(group_by)
    if grouping == "day":
        return None

    grain = "fortnight" if grouping == "fortnight" else "month"

    def stored_period_key(period: Mapping[str, Any]) -> str:
        """Translate display fortnight keys to the persisted F1/F2 format."""
        key = str(period["key"])
        if grain == "fortnight":
            return f"{key[:7]}-F{key[-1:]}"
        return key

    target_periods = _cashflow_periods(period_start, period_end, grouping)
    base_periods = _cashflow_periods(period_start, period_end, grain)
    if not target_periods or not base_periods:
        return None
    stored_base_period_keys = [stored_period_key(period) for period in base_periods]

    state_rows = query_fn(
        """
        SELECT active_preparation_revision, status
        FROM cashflow_preparation_state
        WHERE client_id = %s
        """,
        (client_id,),
    )
    if not state_rows:
        return None
    preparation_revision = int(state_rows[0].get("active_preparation_revision") or 0)
    if preparation_revision <= 0:
        return None

    rows = query_fn(
        """
        SELECT period_key, category_label_snapshot, account_code, account_name,
               treasury_type, debit_total, credit_total, net_total,
               line_count
        FROM cashflow_prepared_rollups
        WHERE client_id = %s
          AND template_id = %s
          AND preparation_revision = %s
          AND period_grain = %s
          AND period_key >= %s
          AND period_key <= %s
        ORDER BY period_key, category_label_snapshot, account_code, treasury_type
        """,
        (
            client_id,
            journal_template_id,
            preparation_revision,
            grain,
            stored_base_period_keys[0],
            stored_base_period_keys[-1],
        ),
    )
    if not rows:
        return None

    target_for_base: dict[str, dict[str, str]] = {}
    for base in base_periods:
        base_start = pd.Timestamp(base["start"]).normalize()
        target = next(
            (
                period
                for period in target_periods
                if pd.Timestamp(period["start"]) <= base_start <= pd.Timestamp(period["end"])
            ),
            None,
        )
        if target is not None:
            target_for_base[stored_period_key(base)] = target

    metric_period_column = "transaction_fortnight" if grain == "fortnight" else "transaction_month"
    voucher_metric_rows = query_fn(
        f"""
        SELECT {metric_period_column} AS period_key, COUNT(DISTINCT voucher_id) AS selected_voucher_count
        FROM cashflow_prepared_lines
        WHERE client_id = %s
          AND template_id = %s
          AND preparation_revision = %s
          AND transaction_date >= %s
          AND transaction_date <= %s
        GROUP BY {metric_period_column}
        """,
        (client_id, journal_template_id, preparation_revision, period_start, period_end),
    )
    selected_vouchers_by_period: dict[str, int] = {}
    for metric_row in voucher_metric_rows:
        target = target_for_base.get(str(metric_row.get("period_key") or ""))
        if target is not None:
            selected_vouchers_by_period[str(target["key"])] = int(metric_row.get("selected_voucher_count") or 0)
    category_buckets: dict[tuple[str, str], float] = {}
    period_metrics = {
        str(period["key"]): {
            "source_line_count": 0,
            "selected_voucher_count": 0,
            "internal_transfer_count": 0,
            "category_review_count": 0,
            "deduction_count": 0,
        }
        for period in target_periods
    }
    source_line_count = 0
    for row in rows:
        target = target_for_base.get(str(row.get("period_key") or ""))
        if target is None:
            continue
        line_count = int(row.get("line_count") or 0)
        source_line_count += line_count
        period_metrics[str(target["key"])]["source_line_count"] += line_count
        period_metrics[str(target["key"])]["selected_voucher_count"] = selected_vouchers_by_period.get(str(target["key"]), 0)
        treasury_type = str(row.get("treasury_type") or "").strip().casefold()
        if treasury_type in {"cash", "bank"}:
            continue
        category = str(row.get("category_label_snapshot") or "").strip()
        if not category:
            category = str(row.get("account_name") or "").strip() or "Unmapped account"
        key = (category, str(target["key"]))
        category_buckets[key] = category_buckets.get(key, 0.0) - float(row.get("net_total") or 0.0)

    period_keys = [str(period["key"]) for period in target_periods]
    categories = sorted({category for category, _period_key in category_buckets})
    category_rows = [
        {
            "category": category,
            **{
                period_key: float(category_buckets.get((category, period_key), 0.0))
                for period_key in period_keys
            },
        }
        for category in categories
    ]
    if category_rows:
        category_totals = pd.DataFrame(category_rows).set_index("category")
        category_totals.index.name = "category"
        category_totals["Total"] = category_totals[period_keys].sum(axis=1)
    else:
        category_totals = pd.DataFrame(columns=[*period_keys, "Total"], dtype=float)
        category_totals.index.name = "category"

    treasury_category_totals = pd.DataFrame(
        [
            {
                "category": category,
                "period_key": period_key,
                "bank_amount": 0.0,
                "cash_amount": 0.0,
                "mixed_amount": float(category_totals.loc[category, period_key]),
                "total_amount": float(category_totals.loc[category, period_key]),
            }
            for category in category_totals.index
            for period_key in period_keys
        ],
        columns=[
            "category",
            "period_key",
            "bank_amount",
            "cash_amount",
            "mixed_amount",
            "total_amount",
        ],
    )
    if view in {"cash", "bank"}:
        metric_period_column = "transaction_fortnight" if grain == "fortnight" else "transaction_month"
        allocation_rows = query_fn(
            f"""
            WITH gateway AS (
                SELECT canonical_occurrence_id,
                       SUM(CASE WHEN LOWER(COALESCE(treasury_type, '')) = 'bank'
                                THEN debit - credit ELSE 0 END) AS bank_movement,
                       SUM(CASE WHEN LOWER(COALESCE(treasury_type, '')) = 'cash'
                                THEN debit - credit ELSE 0 END) AS cash_movement
                FROM cashflow_prepared_lines
                WHERE client_id = %s
                  AND template_id = %s
                  AND preparation_revision = %s
                  AND transaction_date >= %s
                  AND transaction_date <= %s
                GROUP BY canonical_occurrence_id
            ), category_lines AS (
                SELECT canonical_occurrence_id,
                       {metric_period_column} AS period_key,
                       COALESCE(NULLIF(BTRIM(category_label_snapshot), ''),
                                NULLIF(BTRIM(account_name), ''),
                                'Unmapped account') AS category,
                       SUM(debit - credit) AS amount
                FROM cashflow_prepared_lines
                WHERE client_id = %s
                  AND template_id = %s
                  AND preparation_revision = %s
                  AND transaction_date >= %s
                  AND transaction_date <= %s
                  AND LOWER(COALESCE(treasury_type, '')) NOT IN ('cash', 'bank')
                GROUP BY canonical_occurrence_id, {metric_period_column}, category
            )
            SELECT c.category, c.period_key, c.amount,
                   g.bank_movement, g.cash_movement
            FROM category_lines c
            JOIN gateway g ON g.canonical_occurrence_id = c.canonical_occurrence_id
            ORDER BY c.period_key, c.category
            """,
            (
                client_id,
                journal_template_id,
                preparation_revision,
                period_start,
                period_end,
                client_id,
                journal_template_id,
                preparation_revision,
                period_start,
                period_end,
            ),
        )
        allocated: dict[tuple[str, str], dict[str, float]] = {}
        for row in allocation_rows:
            base_period = str(row.get("period_key") or "")
            target = target_for_base.get(base_period)
            if target is None:
                continue
            key = (str(row.get("category") or "Unmapped account"), str(target["key"]))
            bucket = allocated.setdefault(
                key,
                {"bank_amount": 0.0, "cash_amount": 0.0, "mixed_amount": 0.0, "total_amount": 0.0},
            )
            amount = -float(row.get("amount") or 0.0)
            bank_weight = abs(float(row.get("bank_movement") or 0.0))
            cash_weight = abs(float(row.get("cash_movement") or 0.0))
            denominator = bank_weight + cash_weight
            if denominator <= DEFAULT_TOLERANCE:
                bucket["mixed_amount"] += amount
            else:
                bucket["bank_amount"] += amount * bank_weight / denominator
                bucket["cash_amount"] += amount * cash_weight / denominator
            bucket["total_amount"] += amount
        treasury_category_totals = pd.DataFrame(
            [
                {"category": category, "period_key": period_key, **values}
                for (category, period_key), values in allocated.items()
            ],
            columns=[
                "category",
                "period_key",
                "bank_amount",
                "cash_amount",
                "mixed_amount",
                "total_amount",
            ],
        )
    summary = {
        "months": [
            str(period)
            for period in pd.period_range(
                pd.Timestamp(period_start).to_period("M"),
                pd.Timestamp(period_end).to_period("M"),
                freq="M",
            )
        ],
        "periods": target_periods,
        "group_by": grouping,
        "summary_payload_version": 0,
        "category_totals": category_totals,
        "treasury_category_totals": treasury_category_totals,
        "treasury_reconciliation": _cashflow_treasury_reconciliation(
            category_totals,
            treasury_category_totals,
            period_keys,
            tolerance=DEFAULT_TOLERANCE,
        ),
        "treasury_totals_version": CASHFLOW_TREASURY_TOTALS_VERSION,
        "transfer_details": pd.DataFrame(),
        "transfer_reconciliation": None,
        "customer_payment_details": pd.DataFrame(),
        "customer_payment_customer_totals": pd.DataFrame(),
        "customer_payment_period_totals": pd.DataFrame(),
        "customer_payment_reconciliation": {},
        "customer_payment_receipt_categories": [],
        "deduction_details": pd.DataFrame(),
        "categories_needing_review": pd.DataFrame(),
        "selected_vouchers": [],
        "source_line_count": source_line_count,
        "selected_voucher_count": sum(item["selected_voucher_count"] for item in period_metrics.values()),
        "internal_transfer_count": 0,
        "category_review_count": 0,
        "period_metrics": period_metrics,
        "receipt_presentation": receipt_presentation,
        "child_details_prepared": False,
        "child_details_version": 0,
        "cashflow_ledger_rollups": pd.DataFrame(),
        "cashflow_drilldown_controls": {},
        "cashflow_drilldown_prepared": False,
        "drilldown_details_version": 0,
    }
    metadata = {
        "client_id": client_id,
        "journal_template_id": journal_template_id,
        "source_template_ids": [journal_template_id],
        "source_upload_ids": [],
        "selected_template_upload_ids": [],
        "source_upload_selection": "active prepared Cashflows rollups",
        "cashflow_report_source_key": (
            f"prepared-rollups-v1:{client_id}:{journal_template_id}:{preparation_revision}"
        ),
        "cashflow_report_source_mode": "prepared_rollups",
        "preparation_revision": preparation_revision,
        "preparation_status": str(state_rows[0].get("status") or "ready"),
        "summary_read_model": "prepared_rollups",
        "source_row_count": source_line_count,
        "valid_row_count": source_line_count,
        "rejected_row_count": 0,
        "layout_row_count": 0,
        "date_prefilter_applied": True,
        "treasury_filter_stage": f"prepared_rollups_{view}",
        "sql_aggregated": True,
        "raw_json_parsing": False,
        "source_period_start": str(period_start),
        "source_period_end": str(period_end),
    }
    return {"summary": summary, "metadata": metadata}

def load_cashflow_prepared_classification_preview(
    client_id: int,
    template_id: int,
    period_start: Any,
    period_end: Any,
    *,
    page: int = 1,
    limit: int = 50,
    search: str | None = None,
    classification_type: str | None = None,
    account_code: str | None = None,
    category: str | None = None,
    treasury_view: str | None = None,
    query_fn: Callable[..., list[dict[str, Any]]] = execute_query_pg,
) -> dict[str, Any] | None:
    """Read the Data Bank Cashflows classification view from prepared lines.

    ``cashflow_prepared_lines`` is the row-level companion to the report rollups.
    It already contains canonical-voucher selection, Cash/Bank eligibility,
    counter-account inclusion, category snapshots, and derived periods, so the
    Data Bank classification window does not rebuild those decisions.
    """
    if page < 1 or limit < 1 or limit > 200:
        raise ValueError("Invalid classification preview pagination")
    state_rows = query_fn(
        """
        SELECT active_preparation_revision, status
        FROM cashflow_preparation_state
        WHERE client_id = %s
        """,
        (client_id,),
    )
    if not state_rows:
        return None
    preparation_revision = int(state_rows[0].get("active_preparation_revision") or 0)
    if preparation_revision <= 0:
        return None

    where = [
        "p.client_id = %s",
        "p.template_id = %s",
        "p.preparation_revision = %s",
        "p.transaction_date >= %s",
        "p.transaction_date <= %s",
        "cd.is_deleted = FALSE",
    ]
    params: list[Any] = [
        client_id,
        template_id,
        preparation_revision,
        period_start,
        period_end,
    ]

    if search and search.strip():
        like = f"%{search.strip()}%"
        where.append(
            "(LOWER(COALESCE(p.voucher_id, '')) LIKE LOWER(%s) "
            "OR LOWER(COALESCE(p.account_code, '')) LIKE LOWER(%s) "
            "OR LOWER(COALESCE(p.account_name, '')) LIKE LOWER(%s) "
            "OR LOWER(COALESCE(p.category_label_snapshot, '')) LIKE LOWER(%s) "
            "OR EXISTS (SELECT 1 FROM jsonb_each_text(cd.row_data) pair "
            "WHERE LOWER(pair.value) LIKE LOWER(%s)))"
        )
        params.extend([like] * 5)

    if classification_type == "cash":
        where.append("p.treasury_type = 'cash'")
    elif classification_type == "bank":
        where.append("p.treasury_type = 'bank'")
    elif classification_type == "counter":
        where.append("p.classification_status = 'non_treasury'")
    elif classification_type == "review":
        where.append("p.classification_status = 'unmapped'")
    elif classification_type not in (None, ""):
        raise ValueError("Invalid classification type")

    treasury_filter = str(treasury_view or "combined").strip().casefold()
    if treasury_filter not in {"combined", "cash", "bank"}:
        raise ValueError("Invalid treasury view")
    treasury_types = ["cash", "bank"] if treasury_filter == "combined" else [treasury_filter]
    where.append(
        "EXISTS ("
        " SELECT 1 FROM cashflow_prepared_lines treasury"
        " WHERE treasury.client_id = p.client_id"
        "   AND treasury.template_id = p.template_id"
        "   AND treasury.preparation_revision = p.preparation_revision"
        "   AND treasury.canonical_occurrence_id = p.canonical_occurrence_id"
        "   AND treasury.treasury_type = ANY(%s)"
        ")"
    )
    params.append(treasury_types)

    if account_code and account_code.strip():
        where.append("LOWER(p.account_code) = LOWER(%s)")
        params.append(account_code.strip())
    if category and category.strip().casefold() == "__unassigned__":
        where.append("NULLIF(BTRIM(COALESCE(p.category_label_snapshot, '')), '') IS NULL")
    elif category and category.strip():
        where.append("LOWER(COALESCE(p.category_label_snapshot, '')) LIKE LOWER(%s)")
        params.append(f"%{category.strip()}%")

    where_sql = " AND ".join(where)
    from_sql = """
        FROM cashflow_prepared_lines p
        JOIN client_data cd
          ON cd.id = p.source_client_data_id
         AND cd.client_id = p.client_id
    """
    count_rows = query_fn(
        f"SELECT COUNT(*) AS total {from_sql} WHERE {where_sql}",
        tuple(params),
    )
    total = int(count_rows[0].get("total") or 0) if count_rows else 0
    offset = (page - 1) * limit
    rows = query_fn(
        f"""
        SELECT cd.id, cd.row_data, cd.extract_amount, p.transaction_date AS extract_date,
               cd.data_quality_flag, cd.rate_source, p.voucher_id, p.account_code,
               p.account_name, p.transaction_date, p.debit, p.credit, p.treasury_type,
               p.classification_status, p.category_label_snapshot, p.transaction_month,
               p.transaction_fortnight
        {from_sql}
        WHERE {where_sql}
        ORDER BY p.transaction_date DESC NULLS LAST, p.voucher_id, p.account_code, cd.id
        LIMIT %s OFFSET %s
        """,
        tuple([*params, limit, offset]),
    )
    formatted: list[dict[str, Any]] = []
    for row in rows:
        status = row.get("classification_status") or "unmapped"
        treasury_type = str(row.get("treasury_type") or "").casefold()
        if treasury_type in {"cash", "bank"}:
            display_type = treasury_type.title()
        elif status == "non_treasury":
            display_type = "Counter-account"
        else:
            display_type = "Needs review"
        formatted.append({
            "id": row.get("id"),
            "row_data": row.get("row_data") or {},
            "extract_amount": row.get("extract_amount"),
            "extract_date": row.get("extract_date"),
            "data_quality_flag": row.get("data_quality_flag"),
            "rate_source": row.get("rate_source"),
            "classification": {
                "type": display_type,
                "account_code": row.get("account_code"),
                "account_name": row.get("account_name") or "",
                "assigned_category": row.get("category_label_snapshot") or "",
                "classification_status": status,
                "voucher_id": row.get("voucher_id"),
                "transaction_date": row.get("transaction_date"),
                "debit": row.get("debit") or 0,
                "credit": row.get("credit") or 0,
                "configuration_version": None,
                "transaction_month": row.get("transaction_month"),
                "transaction_fortnight": row.get("transaction_fortnight"),
            },
        })
    return {
        "total_rows": total,
        "page": page,
        "limit": limit,
        "rows": formatted,
        "duplicate_upload_suppression": True,
        "source_upload_selection": "active prepared Cashflows revision",
        "source_mode": "prepared_lines",
        "preparation_revision": preparation_revision,
        "preparation_status": str(state_rows[0].get("status") or "ready"),
    }

def load_cashflow_prepared_line_source(
    client_id: int,
    *,
    journal_template_id: int | None,
    period_start: Any,
    period_end: Any,
    query_fn: Callable[..., list[dict[str, Any]]] = execute_query_pg,
) -> LedgerReportSource | None:
    """Load the active revisioned Cashflows lines without rebuilding classifications.

    The preparation service has already applied canonical-voucher selection,
    Cash/Bank eligibility, counter-account inclusion, category snapshots, and
    month/fortnight keys. A missing active revision deliberately returns None
    so the controlled SQL fallback remains available during rollout.
    """
    if journal_template_id is None or period_start is None or period_end is None:
        return None
    state_rows = query_fn(
        """
        SELECT active_preparation_revision, status
        FROM cashflow_preparation_state
        WHERE client_id = %s
        """,
        (client_id,),
    )
    if not state_rows:
        return None
    preparation_revision = int(state_rows[0].get("active_preparation_revision") or 0)
    if preparation_revision <= 0:
        return None
    rows = query_fn(
        """
        SELECT voucher_id, transaction_date, account_code, account_name,
               debit, credit, treasury_type, category_label_snapshot,
               transaction_month, transaction_fortnight, upload_id,
               source_client_data_id
        FROM cashflow_prepared_lines
        WHERE client_id = %s
          AND template_id = %s
          AND preparation_revision = %s
          AND transaction_date >= %s
          AND transaction_date <= %s
        ORDER BY transaction_date, voucher_id, account_code, source_client_data_id
        """,
        (client_id, journal_template_id, preparation_revision, period_start, period_end),
    )
    records = [dict(row) for row in rows]
    journal = pd.DataFrame(records).rename(
        columns={
            "transaction_date": "date",
            "category_label_snapshot": "category",
        }
    )
    for column in (*JOURNAL_COLUMNS, *CASHFLOW_PREPARED_PERIOD_COLUMNS, "treasury_type", "category"):
        if column not in journal:
            journal[column] = ""
    journal = journal.reindex(columns=[*JOURNAL_COLUMNS, "treasury_type", "category", *CASHFLOW_PREPARED_PERIOD_COLUMNS])
    if journal.empty:
        journal = pd.DataFrame(columns=[*JOURNAL_COLUMNS, "treasury_type", "category", *CASHFLOW_PREPARED_PERIOD_COLUMNS])
    else:
        journal["date"] = pd.to_datetime(journal["date"], errors="coerce")
        for column in ("debit", "credit"):
            journal[column] = pd.to_numeric(journal[column], errors="coerce").fillna(0.0)
        journal["voucher_id"] = journal["voucher_id"].map(_text)
        journal["account_code"] = journal["account_code"].map(_canonical_code)
        for column in ("account_name", "voucher_detail", "treasury_type", "category", *CASHFLOW_PREPARED_PERIOD_COLUMNS):
            journal[column] = journal[column].map(_text)
    coa = (
        journal[["account_code", "account_name", "treasury_type", "category"]]
        .drop_duplicates("account_code", keep="last")
        .rename(columns={"account_name": "account_title", "treasury_type": "account_type"})
        .reset_index(drop=True)
        if not journal.empty
        else pd.DataFrame(columns=COA_COLUMNS)
    )
    upload_ids = sorted({int(row["upload_id"]) for row in records if row.get("upload_id") is not None})
    source_client_data_ids = {int(row["source_client_data_id"]) for row in records if row.get("source_client_data_id") is not None}
    metadata = {
        "client_id": client_id,
        "journal_template_id": journal_template_id,
        "source_template_ids": [journal_template_id],
        "source_upload_ids": upload_ids,
        "selected_template_upload_ids": upload_ids,
        "source_upload_selection": "active prepared Cashflows revision",
        "cashflow_report_source_key": f"prepared-v1:{client_id}:{journal_template_id}:{preparation_revision}",
        "cashflow_report_source_mode": "prepared_revision",
        "preparation_revision": preparation_revision,
        "preparation_status": str(state_rows[0].get("status") or "ready"),
        "source_row_count": len(records),
        "valid_row_count": len(records),
        "source_client_data_count": len(source_client_data_ids),
        "rejected_row_count": 0,
        "layout_row_count": 0,
        "date_prefilter_applied": True,
        "source_period_start": str(period_start),
        "source_period_end": str(period_end),
        "treasury_filter_stage": "prepared_revision",
        "sql_aggregated": False,
        "raw_json_parsing": False,
        "prepared_periods_requested": True,
    }
    return LedgerReportSource(journal, coa, (), metadata, ())


def project_cashflow_treasury_view(summary: Mapping[str, Any], treasury_view: str = "combined") -> dict[str, Any]:
    """Project one treasury view from the already-built canonical summary."""
    view = normalize_cashflow_treasury_view(treasury_view)
    combined = summary.get("category_totals")
    if view == "combined":
        return {
            "view": view,
            "totals": combined if isinstance(combined, pd.DataFrame) else pd.DataFrame(),
            "totals_shape": "category",
            "ready": True,
            "reconciliation": summary.get("treasury_reconciliation") or {},
        }

    treasury = summary.get("treasury_category_totals")
    treasury = treasury.copy() if isinstance(treasury, pd.DataFrame) else pd.DataFrame()
    version = int(summary.get("treasury_totals_version", 0) or 0)
    if version < CASHFLOW_TREASURY_TOTALS_VERSION:
        return {
            "view": view,
            "totals": pd.DataFrame(),
            "totals_shape": "split" if view == "split" else "category",
            "ready": False,
            "reconciliation": summary.get("treasury_reconciliation") or {},
        }

    if view == "split":
        return {
            "view": view,
            "totals": treasury,
            "totals_shape": "split",
            "ready": True,
            "reconciliation": summary.get("treasury_reconciliation") or {},
        }

    period_keys = [
        str(period.get("key"))
        for period in summary.get("periods", [])
        if period.get("key") is not None
    ]
    if not period_keys and isinstance(combined, pd.DataFrame):
        period_keys = [str(column) for column in combined.columns if str(column) != "Total"]
    amount_column = "bank_amount" if view == "bank" else "cash_amount"
    category_names = []
    if isinstance(combined, pd.DataFrame) and not combined.empty:
        category_names.extend(str(category) for category in combined.index)
    if not treasury.empty and "category" in treasury.columns:
        category_names.extend(str(category) for category in treasury["category"].dropna().tolist())
    category_names = list(dict.fromkeys(category_names))
    rows: list[dict[str, Any]] = []
    for category in category_names:
        row = {"category": category}
        category_rows = treasury.loc[treasury.get("category", pd.Series(dtype=str)).astype(str).eq(category)] if not treasury.empty else pd.DataFrame()
        for period_key in period_keys:
            period_rows = category_rows.loc[category_rows.get("period_key", pd.Series(dtype=str)).astype(str).eq(period_key)] if not category_rows.empty else pd.DataFrame()
            row[period_key] = float(period_rows[amount_column].sum()) if amount_column in period_rows else 0.0
        row["Total"] = sum(float(row[period_key]) for period_key in period_keys)
        rows.append(row)
    if rows:
        totals = pd.DataFrame(rows).set_index("category")
        totals.index.name = "category"
    else:
        totals = pd.DataFrame(columns=[*period_keys, "Total"], dtype=float)
        totals.index.name = "category"
    return {
        "view": view,
        "totals": totals,
        "totals_shape": "category",
        "ready": True,
        "reconciliation": summary.get("treasury_reconciliation") or {},
    }

def _cashflow_detail_records(frame: Any) -> list[dict[str, Any]]:
    if not isinstance(frame, pd.DataFrame) or frame.empty:
        return []
    return [
        {str(column): _cashflow_json_scalar(value) for column, value in record.items()}
        for record in frame.to_dict("records")
    ]


def _cashflow_detail_frame(payload: Mapping[str, Any], key: str) -> pd.DataFrame:
    records = payload.get(key) or []
    if not isinstance(records, list):
        return pd.DataFrame()
    valid_records = [record for record in records if isinstance(record, Mapping)]
    return pd.DataFrame(valid_records)


def _cashflow_rollup_detail_frame(
    payload: Mapping[str, Any],
    key: str,
    periods: Sequence[Mapping[str, Any]],
) -> pd.DataFrame:
    frame = _cashflow_detail_frame(payload, key)
    if frame.empty or "date" not in frame.columns:
        return frame
    dates = pd.to_datetime(frame["date"], errors="coerce").dt.normalize()
    period_frames: list[pd.DataFrame] = []
    for period in periods:
        start = pd.Timestamp(period["start"]).normalize()
        end = pd.Timestamp(period["end"]).normalize()
        selected = frame.loc[dates.between(start, end, inclusive="both")].copy()
        if selected.empty:
            continue
        selected["period_key"] = period["key"]
        selected["period_label"] = period["label"]
        period_frames.append(selected)
    if not period_frames:
        return frame.iloc[0:0].copy()
    return pd.concat(period_frames, ignore_index=True)


def _cashflow_rollup_ledger_details(
    payload: Mapping[str, Any],
    periods: Sequence[Mapping[str, Any]],
) -> pd.DataFrame:
    """Re-bucket daily prepared ledger rollups into the requested periods."""
    columns = [
        "category",
        "period_key",
        "period_label",
        "account_code",
        "account_name",
        "treasury_type",
        "amount",
        "line_count",
    ]
    frame = _cashflow_detail_frame(payload, "cashflow_ledger_rollups")
    required = set(columns) - {"period_key", "period_label"}
    if frame.empty or not required.issubset(frame.columns) or "period_key" not in frame.columns:
        return pd.DataFrame(columns=columns)

    source_periods = {
        str(period.get("key")): pd.Timestamp(period["start"]).normalize()
        for period in payload.get("periods", [])
        if period.get("key") is not None and period.get("start") is not None
    }
    target_lookup: dict[str, tuple[str, str]] = {}
    for target in periods:
        start = pd.Timestamp(target["start"]).normalize()
        end = pd.Timestamp(target["end"]).normalize()
        for source_key, source_day in source_periods.items():
            if start <= source_day <= end:
                target_lookup[source_key] = (str(target["key"]), str(target["label"]))
    if not target_lookup:
        return pd.DataFrame(columns=columns)

    working = frame.copy()
    target_periods = working["period_key"].astype(str).map(target_lookup)
    selected = working.loc[target_periods.notna()].copy()
    if selected.empty:
        return pd.DataFrame(columns=columns)
    selected[["target_period_key", "target_period_label"]] = pd.DataFrame(
        target_periods.loc[selected.index].tolist(), index=selected.index
    )
    selected["amount"] = pd.to_numeric(selected.get("amount"), errors="coerce")
    selected["line_count"] = pd.to_numeric(selected.get("line_count"), errors="coerce").fillna(0)
    selected = selected.loc[selected["amount"].notna()].copy()
    if selected.empty:
        return pd.DataFrame(columns=columns)
    selected["category"] = selected["category"].fillna("").astype(str)
    selected["account_code"] = selected["account_code"].fillna("").astype(str)
    selected["account_name"] = selected["account_name"].fillna("").astype(str)
    selected["treasury_type"] = selected["treasury_type"].fillna("Unknown").astype(str)
    grouped = (
        selected.groupby(
            [
                "category",
                "target_period_key",
                "target_period_label",
                "account_code",
                "account_name",
                "treasury_type",
            ],
            dropna=False,
            sort=False,
        )
        .agg(amount=("amount", "sum"), line_count=("line_count", "sum"))
        .reset_index()
        .rename(columns={"target_period_key": "period_key", "target_period_label": "period_label"})
    )
    grouped["line_count"] = grouped["line_count"].round().astype(int)
    return grouped[columns]


def _cashflow_drilldown_integrity(summary: Mapping[str, Any]) -> tuple[bool, str | None]:
    """Validate prepared account detail before allowing it into the snapshot."""
    if not bool(summary.get("cashflow_drilldown_prepared")):
        return True, None
    rollups = summary.get("cashflow_ledger_rollups")
    required = {
        "category",
        "period_key",
        "period_label",
        "account_code",
        "account_name",
        "treasury_type",
        "amount",
        "line_count",
    }
    if not isinstance(rollups, pd.DataFrame) or not required.issubset(rollups.columns):
        return False, "drilldown_rollup_schema_invalid"
    key_columns = [
        "category",
        "period_key",
        "period_label",
        "account_code",
        "account_name",
        "treasury_type",
    ]
    if rollups.duplicated(key_columns, keep=False).any():
        return False, "drilldown_rollup_duplicates"
    if rollups["amount"].isna().any() or rollups["line_count"].isna().any():
        return False, "drilldown_rollup_numeric_values_invalid"
    controls = summary.get("cashflow_drilldown_controls") or {}
    category_controls = controls.get("category_controls") if isinstance(controls, Mapping) else None
    if not isinstance(category_controls, list) or any(
        not isinstance(row, Mapping) or str(row.get("status") or "") != "Matched"
        for row in category_controls
    ):
        return False, "drilldown_integrity_variance"
    split = controls.get("cash_bank_split") if isinstance(controls, Mapping) else None
    if isinstance(split, Mapping) and str(split.get("status") or "") != "Matched":
        return False, "drilldown_treasury_split_requires_review"
    return True, None


def _cashflow_summary_payload(summary: Mapping[str, Any]) -> dict[str, Any]:
    from services.ledger_group_summary import (
        _build_cashflow_customer_payment_customer_totals,
        _build_cashflow_customer_payment_reconciliation,
        build_cashflow_transfer_reconciliation,
    )

    periods = [dict(period) for period in summary.get("periods", [])]
    period_keys = [str(period["key"]) for period in periods]
    child_details_prepared = all(
        key in summary
        for key in ("transfer_details", "customer_payment_details", "deduction_details", "categories_needing_review")
    )
    category_totals = summary.get("category_totals")
    categories: list[dict[str, Any]] = []
    if isinstance(category_totals, pd.DataFrame):
        for category, values in category_totals.iterrows():
            categories.append({
                "category": str(category),
                "values": {
                    key: float(values.get(key, 0.0) or 0.0)
                    for key in period_keys
                },
            })
    transfer_details = summary.get("transfer_details")
    transfer_reconciliation = summary.get("transfer_reconciliation")
    if not isinstance(transfer_reconciliation, Mapping):
        transfer_reconciliation = build_cashflow_transfer_reconciliation(transfer_details)
    drilldown_prepared = bool(summary.get('cashflow_drilldown_prepared'))
    drilldown_rollups = summary.get('cashflow_ledger_rollups')
    drilldown_controls = summary.get('cashflow_drilldown_controls') or {}
    return {
        'drilldown_details_prepared': drilldown_prepared,
        'drilldown_details_version': CASHFLOW_DRILLDOWN_DETAILS_VERSION if drilldown_prepared else 0,
        'cashflow_ledger_rollups': _cashflow_detail_records(drilldown_rollups),
        'cashflow_drilldown_controls': drilldown_controls,
        "months": [str(month) for month in summary.get("months", [])],
        "periods": periods,
        "categories": categories,
        "treasury_category_totals": _cashflow_detail_records(summary.get("treasury_category_totals")),
        "treasury_reconciliation": dict(summary.get("treasury_reconciliation") or {}),
        "treasury_totals_version": CASHFLOW_TREASURY_TOTALS_VERSION if "treasury_category_totals" in summary else 0,
        "source_line_count": int(summary.get("source_line_count", 0) or 0),
        "selected_voucher_count": int(summary.get("selected_voucher_count", 0) or 0),
        "internal_transfer_count": int(summary.get("internal_transfer_count", 0) or 0),
        "category_review_count": int(summary.get("category_review_count", 0) or 0),
        "child_details_prepared": child_details_prepared,
        "child_details_version": CASHFLOW_CHILD_DETAILS_VERSION if child_details_prepared else 0,
        "transfer_details": _cashflow_detail_records(transfer_details),
        "transfer_reconciliation": dict(transfer_reconciliation),
        "customer_payment_details": _cashflow_detail_records(summary.get("customer_payment_details")),
        "customer_payment_customer_totals": _cashflow_detail_records(summary.get("customer_payment_customer_totals")),
        "customer_payment_period_totals": _cashflow_detail_records(summary.get("customer_payment_period_totals")),
        "customer_payment_reconciliation": dict(summary.get("customer_payment_reconciliation") or {}),
        "customer_payment_receipt_categories": [str(value) for value in summary.get("customer_payment_receipt_categories") or []],
        "deduction_details": _cashflow_detail_records(summary.get("deduction_details")),
        "categories_needing_review": _cashflow_detail_records(summary.get("categories_needing_review")),
        "period_metrics": summary.get("period_metrics") or {},
        "receipt_presentation": summary.get("receipt_presentation") or "net",
        "summary_payload_version": CASHFLOW_SUMMARY_PAYLOAD_VERSION,
    }


def _cashflow_summary_from_payload(
    payload: Mapping[str, Any],
    group_by: str,
    *,
    include_drilldown: bool = True,
) -> dict[str, Any]:
    from services.ledger_group_summary import (
        _build_cashflow_customer_payment_customer_totals,
        _build_cashflow_customer_payment_period_totals,
        _build_cashflow_customer_payment_reconciliation,
        build_cashflow_transfer_reconciliation,
    )

    periods = [dict(period) for period in payload.get("periods", [])]
    period_keys = [str(period["key"]) for period in periods]
    rows: list[dict[str, Any]] = []
    for category in payload.get("categories", []):
        values = category.get("values") or {}
        row = {"category": str(category.get("category") or "")}
        for key in period_keys:
            row[key] = float(values.get(key, 0.0) or 0.0)
        row["Total"] = sum(float(row[key]) for key in period_keys)
        rows.append(row)
    if rows:
        category_totals = pd.DataFrame(rows).set_index("category")
        category_totals.index.name = "category"
    else:
        category_totals = pd.DataFrame(columns=[*period_keys, "Total"], dtype=float)
        category_totals.index.name = "category"
    treasury_category_totals = _cashflow_detail_frame(payload, "treasury_category_totals")
    treasury_totals_version = int(payload.get("treasury_totals_version", 0) or 0)
    child_details_version = int(payload.get("child_details_version", 0) or 0)
    child_details_prepared = bool(payload.get("child_details_prepared")) and child_details_version >= CASHFLOW_CHILD_DETAILS_VERSION
    transfer_details = _cashflow_detail_frame(payload, "transfer_details") if child_details_prepared else pd.DataFrame()
    customer_payment_details = _cashflow_detail_frame(payload, "customer_payment_details") if child_details_prepared else pd.DataFrame()
    customer_payment_customer_totals = _cashflow_detail_frame(payload, "customer_payment_customer_totals") if child_details_prepared else pd.DataFrame()
    if customer_payment_customer_totals.empty and not customer_payment_details.empty:
        customer_payment_customer_totals = _build_cashflow_customer_payment_customer_totals(customer_payment_details)
    customer_payment_receipt_categories = [str(value) for value in payload.get("customer_payment_receipt_categories") or []]
    customer_payment_reconciliation = dict(payload.get("customer_payment_reconciliation") or {})
    if not customer_payment_reconciliation:
        customer_payment_reconciliation = _build_cashflow_customer_payment_reconciliation(
            customer_payment_details,
            customer_payment_customer_totals,
            category_totals,
            customer_payment_receipt_categories,
            tolerance=0.02,
        )
    customer_payment_period_totals = (
        _build_cashflow_customer_payment_period_totals(
            customer_payment_details,
            periods,
            category_totals,
            customer_payment_receipt_categories,
            tolerance=0.02,
        )
        if child_details_prepared
        else pd.DataFrame()
    )
    drilldown_version = int(payload.get("drilldown_details_version", 0) or 0)
    drilldown_prepared = (
        include_drilldown
        and bool(payload.get("drilldown_details_prepared"))
        and drilldown_version >= CASHFLOW_DRILLDOWN_DETAILS_VERSION
    )
    return {
        "months": [str(month) for month in payload.get("months", [])],
        "periods": periods,
        "group_by": group_by,
        "category_totals": category_totals,
        "cashflow_ledger_rollups": _cashflow_detail_frame(payload, "cashflow_ledger_rollups") if drilldown_prepared else pd.DataFrame(),
        "cashflow_drilldown_controls": payload.get("cashflow_drilldown_controls") or {},
        "cashflow_drilldown_prepared": drilldown_prepared,
        "drilldown_details_version": drilldown_version,
        "treasury_category_totals": treasury_category_totals,
        "treasury_reconciliation": dict(payload.get("treasury_reconciliation") or {}),
        "treasury_totals_version": treasury_totals_version,
        "transfer_details": transfer_details,
        "transfer_reconciliation": (
            payload.get("transfer_reconciliation") or build_cashflow_transfer_reconciliation(transfer_details)
        ) if child_details_prepared else None,
        "customer_payment_details": customer_payment_details,
        "customer_payment_customer_totals": customer_payment_customer_totals,
        "customer_payment_period_totals": customer_payment_period_totals,
        "customer_payment_reconciliation": customer_payment_reconciliation,
        "customer_payment_receipt_categories": customer_payment_receipt_categories,
        "deduction_details": _cashflow_detail_frame(payload, "deduction_details") if child_details_prepared else pd.DataFrame(),
        "categories_needing_review": _cashflow_detail_frame(payload, "categories_needing_review") if child_details_prepared else pd.DataFrame(),
        "selected_vouchers": [],
        "selected_voucher_count": int(payload.get("selected_voucher_count", 0) or 0),
        "internal_transfer_count": int(payload.get("internal_transfer_count", 0) or 0),
        "category_review_count": int(payload.get("category_review_count", 0) or 0),
        "source_line_count": int(payload.get("source_line_count", 0) or 0),
        "period_metrics": payload.get("period_metrics") or {},
        "receipt_presentation": payload.get("receipt_presentation") or "net",
        "summary_payload_version": int(payload.get("summary_payload_version", 0) or 0),
        "child_details_prepared": child_details_prepared,
        "child_details_version": child_details_version,
    }


def _cashflow_json_value(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (TypeError, ValueError):
            return {}
    return value or {}


def _cashflow_preparation_configuration(
    client_id: int,
    query_fn: Callable[..., list[dict[str, Any]]],
) -> dict[str, Any]:
    rows = query_fn(
        """
        SELECT
            COALESCE((
                SELECT json_agg(json_build_object(
                    'account_code', t.account_code,
                    'treasury_type', t.treasury_type
                ) ORDER BY t.treasury_type, t.account_code)
                FROM account_control_cashflow_treasury_accounts t
                WHERE t.client_id = %s AND t.is_active = TRUE
            ), '[]'::json) AS treasury,
            COALESCE((
                SELECT json_build_object(
                    'match_internal_transfers', s.match_internal_transfers,
                    'receipt_presentation', s.receipt_presentation,
                    'source_template_id', s.source_template_id
                )
                FROM account_control_cashflow_settings s
                WHERE s.client_id = %s
                LIMIT 1
            ), '{}'::json) AS settings,
            COALESCE((
                SELECT json_agg(json_build_object(
                    'role_key', r.role_key,
                    'category_id', r.category_id,
                    'label', c.label
                ) ORDER BY r.role_key, r.category_id)
                FROM account_control_cashflow_role_categories r
                JOIN account_control_cashflow_categories c
                  ON c.id = r.category_id
                 AND c.client_id = r.client_id
                 AND c.is_active = TRUE
                WHERE r.client_id = %s
            ), '[]'::json) AS customer_payment_roles
        """,
        (client_id, client_id, client_id),
    )
    row = rows[0] if rows else {}
    treasury = _cashflow_json_value(row.get("treasury"))
    settings = _cashflow_json_value(row.get("settings"))
    role_rows = _cashflow_json_value(row.get("customer_payment_roles"))
    treasury = treasury if isinstance(treasury, list) else []
    settings = settings if isinstance(settings, Mapping) else {}
    role_rows = role_rows if isinstance(role_rows, list) else []
    customer_payment_roles = {
        "customer_receipt": [],
        "bank_charge": [],
        "withholding_tax": [],
        "other_deduction": [],
    }
    for role_row in role_rows:
        role = str(role_row.get("role_key") or "")
        if role in customer_payment_roles and role_row.get("label"):
            customer_payment_roles[role].append(role_row["label"])
    return {
        "source_template_id": settings.get("source_template_id"),
        "cash_account_codes": [
            _canonical_code(item.get("account_code"))
            for item in treasury
            if str(item.get("treasury_type") or "").casefold() == "cash"
        ],
        "bank_account_codes": [
            _canonical_code(item.get("account_code"))
            for item in treasury
            if str(item.get("treasury_type") or "").casefold() == "bank"
        ],
        "match_internal_transfers": bool(settings.get("match_internal_transfers")),
        "receipt_presentation": str(settings.get("receipt_presentation") or "net").casefold(),
        "customer_payment_roles": customer_payment_roles,
    }


def _cashflow_snapshot_bounds(
    client_id: int,
    template_id: int,
    upload_ids: Sequence[int],
    query_fn: Callable[..., list[dict[str, Any]]],
) -> tuple[Any, Any] | None:
    rows = query_fn(
        """
        SELECT MIN(c.transaction_date) AS period_start, MAX(c.transaction_date) AS period_end
        FROM cash_bank_transaction_classifications c
        JOIN client_data cd ON cd.id = c.client_data_id AND cd.is_deleted = FALSE
        WHERE c.client_id = %s
          AND c.template_id = %s
          AND c.upload_id = ANY(%s)
          AND c.is_current = TRUE
          AND c.transaction_date IS NOT NULL
        """,
        (client_id, template_id, list(upload_ids)),
    )
    if not rows or rows[0].get("period_start") is None or rows[0].get("period_end") is None:
        return None
    return rows[0]["period_start"], rows[0]["period_end"]


def _cashflow_daily_rollup(
    payload: Mapping[str, Any],
    period_start: Any,
    period_end: Any,
    group_by: str,
    *,
    include_drilldown: bool = True,
) -> dict[str, Any]:
    from services.ledger_group_summary import (
        DEFAULT_TOLERANCE,
        _build_cashflow_customer_payment_customer_totals,
        _build_cashflow_customer_payment_period_totals,
        _build_cashflow_customer_payment_reconciliation,
        _build_cashflow_drilldown_controls,
        _cashflow_periods,
        _cashflow_treasury_reconciliation,
        build_cashflow_transfer_reconciliation,
        normalize_cashflow_grouping,
    )

    grouping = normalize_cashflow_grouping(group_by)
    periods = _cashflow_periods(period_start, period_end, grouping)
    daily_periods = [
        (str(period.get("key")), pd.Timestamp(period["start"]).normalize())
        for period in payload.get("periods", [])
        if period.get("key") is not None and period.get("start") is not None
    ]
    target_days = {
        period["key"]: [
            key for key, day in daily_periods
            if pd.Timestamp(period["start"]).normalize() <= day <= pd.Timestamp(period["end"]).normalize()
        ]
        for period in periods
    }
    rows: list[dict[str, Any]] = []
    for category in payload.get("categories", []):
        values = category.get("values") or {}
        row = {"category": str(category.get("category") or "")}
        for period in periods:
            row[period["key"]] = sum(float(values.get(day_key, 0.0) or 0.0) for day_key in target_days[period["key"]])
        row["Total"] = sum(float(row[period["key"]]) for period in periods)
        rows.append(row)
    period_metrics: dict[str, dict[str, int]] = {}
    source_metrics = payload.get("period_metrics") or {}
    for period in periods:
        totals = {"source_line_count": 0, "selected_voucher_count": 0, "internal_transfer_count": 0, "category_review_count": 0, "deduction_count": 0}
        for day_key in target_days[period["key"]]:
            values = source_metrics.get(day_key) or {}
            for metric in totals:
                totals[metric] += int(values.get(metric, 0) or 0)
        period_metrics[period["key"]] = totals
    if rows:
        category_totals = pd.DataFrame(rows).set_index("category")
        category_totals.index.name = "category"
    else:
        category_totals = pd.DataFrame(columns=[period["key"] for period in periods] + ["Total"], dtype=float)
        category_totals.index.name = "category"
    source_treasury_totals = _cashflow_detail_frame(payload, "treasury_category_totals")
    category_names = [
        str(category.get("category") or "")
        for category in payload.get("categories", [])
        if isinstance(category, Mapping)
    ]
    treasury_rows: list[dict[str, Any]] = []
    for category in category_names:
        for period in periods:
            selected = source_treasury_totals.loc[
                (source_treasury_totals.get("category", pd.Series(dtype=str)).astype(str) == category)
                & source_treasury_totals.get("period_key", pd.Series(dtype=str)).astype(str).isin(target_days[period["key"]])
            ] if not source_treasury_totals.empty else pd.DataFrame()
            treasury_rows.append({
                "category": category,
                "period_key": period["key"],
                "bank_amount": float(selected["bank_amount"].sum()) if "bank_amount" in selected else 0.0,
                "cash_amount": float(selected["cash_amount"].sum()) if "cash_amount" in selected else 0.0,
                "mixed_amount": float(selected["mixed_amount"].sum()) if "mixed_amount" in selected else 0.0,
                "total_amount": float(selected["total_amount"].sum()) if "total_amount" in selected else 0.0,
            })
    treasury_category_totals = pd.DataFrame(treasury_rows, columns=["category", "period_key", "bank_amount", "cash_amount", "mixed_amount", "total_amount"])
    treasury_reconciliation = _cashflow_treasury_reconciliation(
        category_totals,
        treasury_category_totals,
        [str(period["key"]) for period in periods],
        tolerance=DEFAULT_TOLERANCE,
    )
    totals = {
        metric: sum(values.get(metric, 0) for values in period_metrics.values())
        for metric in ("source_line_count", "selected_voucher_count", "internal_transfer_count", "category_review_count", "deduction_count")
    }
    child_details_version = int(payload.get("child_details_version", 0) or 0)
    child_details_prepared = bool(payload.get("child_details_prepared")) and child_details_version >= CASHFLOW_CHILD_DETAILS_VERSION
    transfer_details = _cashflow_rollup_detail_frame(payload, "transfer_details", periods) if child_details_prepared else pd.DataFrame()
    customer_payment_details = _cashflow_rollup_detail_frame(payload, "customer_payment_details", periods) if child_details_prepared else pd.DataFrame()
    customer_payment_customer_totals = _build_cashflow_customer_payment_customer_totals(customer_payment_details)
    deduction_details = _cashflow_rollup_detail_frame(payload, "deduction_details", periods) if child_details_prepared else pd.DataFrame()
    review = _cashflow_rollup_detail_frame(payload, "categories_needing_review", periods) if child_details_prepared else pd.DataFrame()
    transfer_reconciliation = build_cashflow_transfer_reconciliation(transfer_details) if child_details_prepared else None
    customer_payment_receipt_categories = [str(value) for value in payload.get("customer_payment_receipt_categories") or []]
    customer_payment_reconciliation = _build_cashflow_customer_payment_reconciliation(
        customer_payment_details,
        customer_payment_customer_totals,
        category_totals,
        customer_payment_receipt_categories,
        tolerance=DEFAULT_TOLERANCE,
    )
    customer_payment_period_totals = _build_cashflow_customer_payment_period_totals(
        customer_payment_details,
        periods,
        category_totals,
        customer_payment_receipt_categories,
        tolerance=DEFAULT_TOLERANCE,
    ) if child_details_prepared else pd.DataFrame()
    if child_details_prepared:
        totals["internal_transfer_count"] = len(transfer_details)
        totals["category_review_count"] = len(review)
    drilldown_version = int(payload.get("drilldown_details_version", 0) or 0)
    drilldown_prepared = (
        include_drilldown
        and bool(payload.get("drilldown_details_prepared"))
        and drilldown_version >= CASHFLOW_DRILLDOWN_DETAILS_VERSION
    )
    ledger_rollups = (
        _cashflow_rollup_ledger_details(payload, periods)
        if drilldown_prepared
        else pd.DataFrame()
    )
    drilldown_controls = (
        _build_cashflow_drilldown_controls(category_totals, ledger_rollups, tolerance=DEFAULT_TOLERANCE)
        if drilldown_prepared
        else {}
    )
    months = [
        str(period)
        for period in pd.period_range(pd.Timestamp(period_start).to_period("M"), pd.Timestamp(period_end).to_period("M"), freq="M")
    ]
    return {
        "months": months,
        "periods": periods,
        "group_by": grouping,
        "summary_payload_version": int(payload.get("summary_payload_version", 0) or 0),
        "category_totals": category_totals,
        "treasury_category_totals": treasury_category_totals,
        "treasury_reconciliation": treasury_reconciliation,
        "treasury_totals_version": int(payload.get("treasury_totals_version", 0) or 0),
        "transfer_details": transfer_details,
        "transfer_reconciliation": transfer_reconciliation,
        "customer_payment_details": customer_payment_details,
        "customer_payment_customer_totals": customer_payment_customer_totals,
        "customer_payment_period_totals": customer_payment_period_totals,
        "customer_payment_reconciliation": customer_payment_reconciliation,
        "customer_payment_receipt_categories": customer_payment_receipt_categories,
        "deduction_details": deduction_details,
        "categories_needing_review": review,
        "selected_vouchers": [],
        "source_line_count": totals["source_line_count"],
        "selected_voucher_count": totals["selected_voucher_count"],
        "internal_transfer_count": totals["internal_transfer_count"],
        "category_review_count": totals["category_review_count"],
        "period_metrics": period_metrics,
        "receipt_presentation": payload.get("receipt_presentation") or "net",
        "summary_payload_version": int(payload.get("summary_payload_version", 0) or 0),
        "child_details_prepared": child_details_prepared,
        "child_details_version": child_details_version,
        "cashflow_ledger_rollups": ledger_rollups,
        "cashflow_drilldown_controls": drilldown_controls,
        "cashflow_drilldown_prepared": drilldown_prepared,
        "drilldown_details_version": drilldown_version,
    }


def get_cashflow_report_revision(
    client_id: int,
    *,
    query_fn: Callable[..., list[dict[str, Any]]] = execute_query_pg,
) -> int:
    """Return the durable application revision for one client's Cashflows data."""
    rows = query_fn(
        "SELECT revision FROM cashflow_report_revisions WHERE client_id = %s",
        (client_id,),
    )
    if not rows:
        return 1
    return max(int(rows[0].get("revision") or 1), 1)


def advance_cashflow_report_revision(
    client_id: int,
    *,
    reason: str,
    changed_by: str | None = None,
    write_fn: Callable[..., Any] = execute_write_pg,
) -> int:
    """Refresh the compatibility marker without creating another report copy."""
    revision = write_fn(
        """
        INSERT INTO cashflow_report_revisions (client_id, revision, changed_at, changed_by, change_reason)
        VALUES (%s, 2, CURRENT_TIMESTAMP, %s, %s)
        ON CONFLICT (client_id) DO UPDATE SET
            changed_at = CURRENT_TIMESTAMP,
            changed_by = EXCLUDED.changed_by,
            change_reason = EXCLUDED.change_reason
        RETURNING revision
        """,
        (client_id, changed_by, reason),
    )
    return max(int(revision or 1), 1)

def load_cashflow_prepared_summary(
    client_id: int,
    *,
    journal_template_id: int | None,
    period_start: Any,
    period_end: Any,
    group_by: str,
    receipt_presentation: str,
    report_revision: int | None = None,
    query_fn: Callable[..., list[dict[str, Any]]] = execute_query_pg,
    include_drilldown: bool = False,
) -> dict[str, Any] | None:
    """Roll a full-range daily snapshot into the requested report periods."""
    if journal_template_id is None:
        return None
    source_identity = select_cashflow_report_source_identity(client_id, journal_template_id, query_fn)
    if not source_identity.ready:
        return None
    upload_ids = list(source_identity.upload_ids)
    bounds = _cashflow_snapshot_bounds(client_id, journal_template_id, upload_ids, query_fn)
    if bounds is None:
        return None
    source_start, source_end = bounds
    query_params: list[Any] = [
        client_id,
        journal_template_id,
        source_identity.source_key,
        source_start,
        source_end,
        receipt_presentation,
    ]
    revision_filter = ""
    if report_revision is not None:
        revision_filter = "          AND report_revision = %s\n"
        query_params.append(int(report_revision))
    query = """
        SELECT summary_payload, configuration_version, report_revision, created_at
        FROM cash_bank_summary_read_models
        WHERE client_id = %s
          AND template_id = %s
          AND source_upload_ids_key = %s
          AND period_start = %s
          AND period_end = %s
          AND group_by = 'day'
          AND receipt_presentation = %s
        ORDER BY created_at DESC
        LIMIT 1
        """
    query = query.replace(
        "          AND receipt_presentation = %s\n",
        "          AND receipt_presentation = %s\n" + revision_filter,
    )
    rows = query_fn(query, tuple(query_params))
    if not rows:
        return None
    payload = _cashflow_json_value(rows[0].get("summary_payload"))
    if not isinstance(payload, Mapping):
        return None
    summary = _cashflow_daily_rollup(
        payload,
        period_start,
        period_end,
        group_by,
        include_drilldown=include_drilldown,
    )
    metadata = {
        "client_id": client_id,
        "journal_template_id": journal_template_id,
        "source_template_ids": [journal_template_id],
        "source_upload_ids": upload_ids,
        "selected_template_upload_ids": upload_ids,
        "cashflow_report_source_key": source_identity.source_key,
        "cashflow_report_source_mode": source_identity.selection_mode,
        "duplicate_upload_suppression": source_identity.selection_mode == "latest_voucher",
        "duplicate_uploads_suppressed": source_identity.duplicate_uploads_suppressed,
        "source_row_count": int(summary.get("source_line_count", 0) or 0),
        "valid_row_count": int(summary.get("source_line_count", 0) or 0),
        "rejected_row_count": 0,
        "layout_row_count": 0,
        "date_prefilter_applied": True,
        "source_period_start": str(period_start),
        "source_period_end": str(period_end),
        "treasury_filter_stage": "summary_prepared_snapshot",
        "sql_aggregated": True,
        "raw_json_parsing": False,
        "summary_read_model": "prepared_snapshot",
        "summary_payload_version": int(summary.get("summary_payload_version", 0) or 0),
        "treasury_totals_version": int(summary.get("treasury_totals_version", 0) or 0),
        "child_details_prepared": bool(summary.get("child_details_prepared")),
        "child_details_version": int(summary.get("child_details_version", 0) or 0),
        "drilldown_details_prepared": bool(summary.get("cashflow_drilldown_prepared")),
        "drilldown_details_version": int(summary.get("drilldown_details_version", 0) or 0),
        "classification_configuration_version": rows[0].get("configuration_version"),
        "cashflow_report_revision": max(int(rows[0].get("report_revision") or 1), 1),
        "summary_read_model_created_at": rows[0].get("created_at"),
    }
    return {"summary": summary, "metadata": metadata}


def invalidate_cashflow_summary_read_models(
    client_id: int,
    *,
    reason: str = "cashflow_configuration_changed",
    changed_by: str | None = None,
    write_fn: Callable[..., Any] = execute_write_pg,
) -> int:
    """Invalidate read models and advance the client revision after a committed change."""
    write_fn(
        "DELETE FROM cash_bank_summary_read_models WHERE client_id = %s",
        (client_id,),
    )
    return advance_cashflow_report_revision(
        client_id,
        reason=reason,
        changed_by=changed_by,
        write_fn=write_fn,
    )

def prepare_cashflow_summary_snapshot(
    client_id: int,
    *,
    query_fn: Callable[..., list[dict[str, Any]]] = execute_query_pg,
    write_fn: Callable[..., Any] = execute_write_pg,
) -> dict[str, Any]:
    """Build one reusable daily snapshot after committed source/configuration changes."""
    from services.ledger_group_summary import build_cashflow_summary

    configuration = _cashflow_preparation_configuration(client_id, query_fn)
    template_id = configuration.get("source_template_id")
    if template_id is None:
        return {"status": "skipped", "reason": "template_not_configured"}
    source_identity = select_cashflow_report_source_identity(client_id, int(template_id), query_fn)
    if not source_identity.ready:
        return {"status": "skipped", "reason": "no_source_uploads"}
    upload_ids = list(source_identity.upload_ids)
    bounds = _cashflow_snapshot_bounds(client_id, int(template_id), upload_ids, query_fn)
    if bounds is None:
        return {"status": "skipped", "reason": "no_classified_dates"}
    source_start, source_end = bounds
    report_revision = get_cashflow_report_revision(client_id, query_fn=query_fn)
    source = load_cashflow_sql_source(
        client_id,
        journal_template_id=int(template_id),
        period_start=source_start,
        period_end=source_end,
        cashflow_group_by="day",
        query_fn=query_fn,
    )
    if source is None or not source.ready:
        return {"status": "skipped", "reason": "source_not_ready"}
    summary = build_cashflow_summary(
        source.journal,
        source.coa,
        configuration["cash_account_codes"],
        configuration["bank_account_codes"],
        source_start,
        source_end,
        match_internal_transfers=configuration["match_internal_transfers"],
        receipt_presentation=configuration["receipt_presentation"],
        cashflow_role_categories=configuration["customer_payment_roles"],
        group_by="day",
        include_drilldown=True,
    )
    valid, reason = _cashflow_drilldown_integrity(summary)
    if not valid:
        return {"status": "skipped", "reason": reason}
    store_cashflow_summary_read_model(
        client_id,
        journal_template_id=int(template_id),
        source_upload_ids=source.metadata.get("selected_template_upload_ids") or upload_ids,
        source_key=source.metadata.get("cashflow_report_source_key") or source_identity.source_key,
        configuration_version=source.metadata.get("classification_configuration_version") or 1,
        report_revision=report_revision,
        period_start=source_start,
        period_end=source_end,
        group_by="day",
        summary=summary,
        write_fn=write_fn,
        write_drilldown_projection=True,
    )
    return {"status": "prepared", "period_start": str(source_start), "period_end": str(source_end), "upload_count": len(upload_ids)}

def load_cashflow_summary_read_model(
    client_id: int,
    *,
    journal_template_id: int | None,
    period_start: Any,
    period_end: Any,
    group_by: str,
    receipt_presentation: str,
    report_revision: int | None = None,
    query_fn: Callable[..., list[dict[str, Any]]] = execute_query_pg,
    include_drilldown: bool = False,
) -> dict[str, Any] | None:
    """Load an exact summary-only snapshot when the source/configuration is unchanged."""
    if journal_template_id is None:
        return None
    source_identity = select_cashflow_report_source_identity(client_id, journal_template_id, query_fn)
    if not source_identity.ready:
        return None
    upload_ids = list(source_identity.upload_ids)
    upload_key = source_identity.source_key
    query_params: list[Any] = [
        client_id,
        journal_template_id,
        upload_key,
        period_start,
        period_end,
        group_by,
        receipt_presentation,
    ]
    revision_filter = ""
    if report_revision is not None:
        revision_filter = "          AND report_revision = %s\n"
        query_params.append(int(report_revision))
    query = """
        SELECT summary_payload, configuration_version, report_revision, created_at
        FROM cash_bank_summary_read_models
        WHERE client_id = %s
          AND template_id = %s
          AND source_upload_ids_key = %s
          AND period_start = %s
          AND period_end = %s
          AND group_by = %s
          AND receipt_presentation = %s
        ORDER BY created_at DESC
        LIMIT 1
        """
    query = query.replace(
        "          AND receipt_presentation = %s\n",
        "          AND receipt_presentation = %s\n" + revision_filter,
    )
    rows = query_fn(query, tuple(query_params))
    if not rows:
        return None
    payload = rows[0].get("summary_payload") or {}
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except (TypeError, ValueError):
            return None
    if not isinstance(payload, Mapping):
        return None
    summary = _cashflow_summary_from_payload(payload, group_by, include_drilldown=include_drilldown)
    drilldown_version = int(payload.get('drilldown_details_version', 0) or 0)
    drilldown_prepared = (
        include_drilldown
        and bool(payload.get('drilldown_details_prepared'))
        and drilldown_version >= CASHFLOW_DRILLDOWN_DETAILS_VERSION
    )
    summary['cashflow_ledger_rollups'] = (
        _cashflow_detail_frame(payload, 'cashflow_ledger_rollups') if include_drilldown and drilldown_prepared else pd.DataFrame()
    )
    summary['cashflow_drilldown_controls'] = payload.get('cashflow_drilldown_controls') or {}
    summary['cashflow_drilldown_prepared'] = drilldown_prepared
    summary['drilldown_details_version'] = drilldown_version
    metadata = {
        'drilldown_details_prepared': drilldown_prepared,
        'drilldown_details_version': drilldown_version,
        "client_id": client_id,
        "journal_template_id": journal_template_id,
        "source_template_ids": [journal_template_id],
        "source_upload_ids": upload_ids,
        "selected_template_upload_ids": upload_ids,
        "cashflow_report_source_key": source_identity.source_key,
        "cashflow_report_source_mode": source_identity.selection_mode,
        "duplicate_upload_suppression": source_identity.selection_mode == "latest_voucher",
        "duplicate_uploads_suppressed": source_identity.duplicate_uploads_suppressed,
        "source_row_count": int(payload.get("source_line_count", 0) or 0),
        "valid_row_count": int(payload.get("source_line_count", 0) or 0),
        "rejected_row_count": 0,
        "layout_row_count": 0,
        "date_prefilter_applied": True,
        "source_period_start": str(period_start),
        "source_period_end": str(period_end),
        "treasury_filter_stage": "summary_read_model",
        "sql_aggregated": True,
        "raw_json_parsing": False,
        "summary_read_model": "hit",
        "summary_payload_version": int(payload.get("summary_payload_version", 0) or 0),
        "treasury_totals_version": int(payload.get("treasury_totals_version", 0) or 0),
        "child_details_prepared": bool(payload.get("child_details_prepared")),
        "child_details_version": int(payload.get("child_details_version", 0) or 0),
        "drilldown_details_prepared": drilldown_prepared,
        "drilldown_details_version": drilldown_version,
        "classification_configuration_version": rows[0].get("configuration_version"),
        "cashflow_report_revision": max(int(rows[0].get("report_revision") or 1), 1),
        "summary_read_model_created_at": rows[0].get("created_at"),
    }
    return {"summary": summary, "metadata": metadata}



def _cashflow_projection_number(value: Any) -> float:
    try:
        if value is None or pd.isna(value):
            return 0.0
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _cashflow_drilldown_projection_records(
    summary: Mapping[str, Any],
    *,
    source_group_by: str,
) -> list[dict[str, Any]]:
    """Convert validated prepared daily rollups into indexed projection rows."""
    if not bool(summary.get("cashflow_drilldown_prepared")):
        return []
    rollups = summary.get("cashflow_ledger_rollups")
    if not isinstance(rollups, pd.DataFrame) or rollups.empty:
        return []
    periods = {
        str(period.get("key")): period
        for period in summary.get("periods", [])
        if isinstance(period, Mapping) and period.get("key") is not None
    }
    category_totals = summary.get("category_totals")
    records: list[dict[str, Any]] = []
    for row in _cashflow_detail_records(rollups):
        period_key = str(row.get("period_key") or "")
        period = periods.get(period_key)
        category = str(row.get("category") or "")
        if period is None or not category:
            continue
        category_total = 0.0
        if isinstance(category_totals, pd.DataFrame) and category in category_totals.index:
            category_total = _cashflow_projection_number(category_totals.loc[category].get(period_key, 0.0))
        records.append({
            "source_group_by": str(source_group_by),
            "period_start": str(period.get("start") or ""),
            "period_end": str(period.get("end") or ""),
            "period_key": period_key,
            "period_label": str(period.get("label") or period_key),
            "category": category,
            "account_code": str(row.get("account_code") or ""),
            "account_name": str(row.get("account_name") or ""),
            "treasury_type": str(row.get("treasury_type") or "Unknown"),
            "amount": _cashflow_projection_number(row.get("amount")),
            "line_count": int(round(_cashflow_projection_number(row.get("line_count")))),
            "category_total": category_total,
        })
    # The source rollup can contain more than one row for the same account
    # code/treasury bucket on a day (for example, duplicate source lines with
    # different display names). Collapse those rows before the bulk upsert so
    # PostgreSQL never receives the same ON CONFLICT key twice in one command.
    deduplicated: dict[tuple[str, str, str, str, str], dict[str, Any]] = {}
    for record in records:
        key = (
            record["source_group_by"],
            record["period_key"],
            record["category"],
            record["account_code"],
            record["treasury_type"],
        )
        existing = deduplicated.get(key)
        if existing is None:
            deduplicated[key] = record
            continue
        existing["amount"] += record["amount"]
        existing["line_count"] += record["line_count"]
        if not existing["account_name"] and record["account_name"]:
            existing["account_name"] = record["account_name"]
    return list(deduplicated.values())


def _store_cashflow_drilldown_projection(
    client_id: int,
    *,
    journal_template_id: int,
    source_upload_ids: Sequence[int],
    source_key: str,
    configuration_version: int | None,
    report_revision: int,
    summary: Mapping[str, Any],
    write_fn: Callable[..., Any],
) -> int:
    records = _cashflow_drilldown_projection_records(summary, source_group_by="day")
    if not records or not source_upload_ids or not source_key:
        return 0
    upload_key = source_key
    columns = [
        "client_id", "template_id", "source_upload_ids_key", "configuration_version", "report_revision",
        "source_group_by", "period_start", "period_end", "period_key", "period_label", "category",
        "account_code", "account_name", "treasury_type", "amount", "line_count", "category_total",
    ]
    update_columns = [
        "configuration_version", "period_end", "period_label", "account_name", "amount",
        "line_count", "category_total", "created_at",
    ]
    conflict_columns = [
        "client_id", "template_id", "source_upload_ids_key", "report_revision", "source_group_by",
        "period_key", "category", "account_code", "treasury_type",
    ]
    for offset in range(0, len(records), 250):
        chunk = records[offset:offset + 250]
        placeholders = ", ".join("(" + ", ".join(["%s"] * len(columns)) + ")" for _ in chunk)
        params: list[Any] = []
        for record in chunk:
            params.extend([
                client_id,
                journal_template_id,
                upload_key,
                int(configuration_version or 1),
                int(report_revision or 1),
                record["source_group_by"],
                record["period_start"],
                record["period_end"],
                record["period_key"],
                record["period_label"],
                record["category"],
                record["account_code"],
                record["account_name"],
                record["treasury_type"],
                record["amount"],
                record["line_count"],
                record["category_total"],
            ])
        query = f"""
            INSERT INTO cashflow_ledger_rollup_read_models ({', '.join(columns)})
            VALUES {placeholders}
            ON CONFLICT ({', '.join(conflict_columns)})
            DO UPDATE SET {', '.join(f"{column} = EXCLUDED.{column}" for column in update_columns)}
        """
        write_fn(query, tuple(params))
    return len(records)


def store_cashflow_summary_read_model(
    client_id: int,
    *,
    journal_template_id: int | None,
    source_upload_ids: Sequence[int],
    configuration_version: int | None,
    period_start: Any,
    period_end: Any,
    group_by: str,
    summary: Mapping[str, Any],
    report_revision: int | None = None,
    source_key: str | None = None,
    write_fn: Callable[..., Any] = execute_write_pg,
    write_drilldown_projection: bool = False,
) -> None:
    """Persist the replaceable current summary cache without legacy rollup copies.

    Current Cashflows detail and drill-down data come from the prepared lines and
    prepared rollups. The old cashflow_ledger_rollup_read_models projection is
    retained for later cleanup but is no longer written by new reports.
    """
    if journal_template_id is None or not source_upload_ids:
        return
    payload = _cashflow_summary_payload(summary)
    source_cache_key = source_key or _cashflow_upload_ids_key(source_upload_ids)
    write_fn(
        """
        INSERT INTO cash_bank_summary_read_models (
            client_id, template_id, source_upload_ids_key, configuration_version, report_revision,
            period_start, period_end, group_by, receipt_presentation, summary_payload
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
        ON CONFLICT (client_id, template_id, source_upload_ids_key, period_start, period_end, group_by, receipt_presentation)
        DO UPDATE SET
            configuration_version = EXCLUDED.configuration_version,
            report_revision = EXCLUDED.report_revision,
            summary_payload = EXCLUDED.summary_payload,
            created_at = CURRENT_TIMESTAMP
        """,
        (
            client_id,
            journal_template_id,
            source_cache_key,
            int(configuration_version or 1),
            int(report_revision or 1),
            period_start,
            period_end,
            group_by,
            payload["receipt_presentation"],
            json.dumps(payload),
        ),
    )
    # The legacy drill-down projection is intentionally not written. It used
    # report_revision in its conflict key and therefore accumulated full report
    # copies. Prepared lines/rollups are the current-only detail source.

def load_cashflow_prepared_drilldown(
    client_id: int,
    *,
    journal_template_id: int | None,
    period_start: Any,
    period_end: Any,
    group_by: str,
    category: str,
    report_revision: int | None = None,
    treasury_view: str = "combined",
    query_fn: Callable[..., list[dict[str, Any]]] = execute_query_pg,
) -> dict[str, Any] | None:
    """Load one category from the active row-level Cashflows prepared model.

    The query keeps the voucher-level treasury leg when grouping counter lines,
    so drill-down remains faithful to the report population while avoiding the
    legacy rollup read-model table. ``None`` deliberately preserves the
    controlled fallback used during rollout or a failed preparation revision.
    """
    from services.ledger_group_summary import _cashflow_periods, normalize_cashflow_grouping

    category_text = str(category or "").strip()
    if journal_template_id is None or not category_text:
        return None
    view = normalize_cashflow_treasury_view(treasury_view)
    if view == "split":
        return None

    state_rows = query_fn(
        """
        SELECT active_preparation_revision, status
        FROM cashflow_preparation_state
        WHERE client_id = %s
        """,
        (client_id,),
    )
    if not state_rows:
        return None
    preparation_revision = int(state_rows[0].get("active_preparation_revision") or 0)
    if preparation_revision <= 0:
        return None

    grouping = normalize_cashflow_grouping(group_by)
    periods = _cashflow_periods(period_start, period_end, grouping)
    if not periods:
        return None

    rows = query_fn(
        """
        WITH gateway AS (
            SELECT canonical_occurrence_id,
                   CASE
                       WHEN MAX(CASE WHEN LOWER(COALESCE(treasury_type, '')) = 'bank' THEN 1 ELSE 0 END) = 1
                        AND MAX(CASE WHEN LOWER(COALESCE(treasury_type, '')) = 'cash' THEN 1 ELSE 0 END) = 1
                           THEN 'Bank, Cash'
                       WHEN MAX(CASE WHEN LOWER(COALESCE(treasury_type, '')) = 'bank' THEN 1 ELSE 0 END) = 1
                           THEN 'Bank'
                       WHEN MAX(CASE WHEN LOWER(COALESCE(treasury_type, '')) = 'cash' THEN 1 ELSE 0 END) = 1
                           THEN 'Cash'
                       ELSE 'Unknown'
                   END AS treasury_type
            FROM cashflow_prepared_lines
            WHERE client_id = %s
              AND template_id = %s
              AND preparation_revision = %s
              AND transaction_date >= %s
              AND transaction_date <= %s
            GROUP BY canonical_occurrence_id
        ), counter_lines AS (
            SELECT p.transaction_date::date AS period_start,
                   COALESCE(NULLIF(BTRIM(p.category_label_snapshot), ''),
                            NULLIF(BTRIM(p.account_name), ''),
                            'Unmapped account') AS category,
                   p.account_code,
                   COALESCE(NULLIF(BTRIM(p.account_name), ''), '') AS account_name,
                   g.treasury_type,
                   SUM(p.credit - p.debit) AS amount,
                   COUNT(*) AS line_count
            FROM cashflow_prepared_lines p
            JOIN gateway g ON g.canonical_occurrence_id = p.canonical_occurrence_id
            WHERE p.client_id = %s
              AND p.template_id = %s
              AND p.preparation_revision = %s
              AND p.transaction_date >= %s
              AND p.transaction_date <= %s
              AND LOWER(COALESCE(p.treasury_type, '')) NOT IN ('cash', 'bank')
            GROUP BY p.transaction_date::date,
                     COALESCE(NULLIF(BTRIM(p.category_label_snapshot), ''),
                              NULLIF(BTRIM(p.account_name), ''),
                              'Unmapped account'),
                     p.account_code,
                     COALESCE(NULLIF(BTRIM(p.account_name), ''), ''),
                     g.treasury_type
        )
        SELECT period_start, category, account_code, account_name,
               treasury_type, amount, line_count
        FROM counter_lines
        WHERE category = %s
        ORDER BY period_start, category, account_code, treasury_type
        """,
        (
            client_id,
            journal_template_id,
            preparation_revision,
            period_start,
            period_end,
            client_id,
            journal_template_id,
            preparation_revision,
            period_start,
            period_end,
            category_text,
        ),
    )
    if not rows:
        return None

    frame = pd.DataFrame(rows)
    required = {
        "period_start", "category", "account_code", "account_name",
        "treasury_type", "amount", "line_count",
    }
    if not required.issubset(frame.columns):
        return None
    frame["period_start"] = pd.to_datetime(frame["period_start"], errors="coerce").dt.normalize()
    frame["amount"] = pd.to_numeric(frame["amount"], errors="coerce")
    frame["line_count"] = pd.to_numeric(frame["line_count"], errors="coerce").fillna(0)
    frame = frame.loc[frame["period_start"].notna() & frame["amount"].notna()].copy()
    if frame.empty:
        return None

    period_keys: list[str | None] = []
    period_labels: list[str | None] = []
    for value in frame["period_start"]:
        match = next(
            (
                period
                for period in periods
                if pd.Timestamp(period["start"]) <= value <= pd.Timestamp(period["end"])
            ),
            None,
        )
        period_keys.append(str(match["key"]) if match else None)
        period_labels.append(str(match["label"]) if match else None)
    frame["period_key"] = period_keys
    frame["period_label"] = period_labels
    frame = frame.loc[frame["period_key"].notna()].copy()
    if view in {"cash", "bank"}:
        wanted = "Cash" if view == "cash" else "Bank"
        frame = frame.loc[frame["treasury_type"].astype(str).str.casefold().eq(wanted.casefold())].copy()
    if frame.empty:
        return None

    frame["category"] = frame["category"].fillna("").astype(str)
    frame["account_code"] = frame["account_code"].fillna("").astype(str)
    frame["account_name"] = frame["account_name"].fillna("").astype(str)
    frame["treasury_type"] = frame["treasury_type"].fillna("Unknown").astype(str)
    grouped = (
        frame.groupby(
            ["category", "period_key", "period_label", "account_code", "account_name", "treasury_type"],
            dropna=False,
            sort=False,
        )
        .agg(amount=("amount", "sum"), line_count=("line_count", "sum"))
        .reset_index()
    )
    if grouped.empty:
        return None
    grouped["line_count"] = grouped["line_count"].round().astype(int)
    output_columns = [
        "category", "period_key", "period_label", "account_code", "account_name",
        "treasury_type", "amount", "line_count",
    ]
    detail_rows = _cashflow_detail_records(grouped[output_columns])
    detail_total = sum(_cashflow_projection_number(row.get("amount")) for row in detail_rows)
    return {
        "rows": detail_rows,
        "controls": {
            "category_controls": [{"category": category_text, "status": "Matched"}],
            "cash_bank_split": {"status": "Matched", "detail_total": detail_total},
        },
        "metadata": {
            "targeted_prepared_lines": True,
            "summary_read_model": "prepared_lines",
            "sql_aggregated": True,
            "raw_json_parsing": False,
            "client_id": client_id,
            "journal_template_id": journal_template_id,
            "source_upload_ids": [],
            "cashflow_report_source_key": f"prepared-v1:{client_id}:{journal_template_id}:{preparation_revision}",
            "cashflow_report_source_mode": "prepared_lines",
            "preparation_revision": preparation_revision,
            "preparation_status": str(state_rows[0].get("status") or "ready"),
            "cashflow_report_revision": max(int(report_revision or 1), 1),
            "source_row_count": int(frame["line_count"].sum()),
            "treasury_filter_stage": f"prepared_lines_{view}",
        },
    }

def load_cashflow_sql_source(
    client_id: int,
    *,
    journal_upload_id: int | None = None,
    journal_template_id: int | None = None,
    period_start: Any = None,
    period_end: Any = None,
    cashflow_group_by: str | None = None,
    query_fn: Callable[..., list[dict[str, Any]]] = execute_query_pg,
) -> LedgerReportSource | None:
    """Load Cashflows from the SQL-aggregated derived classification layer.

    The query filters dates and current classification rows in SQL, joins the
    counter-account lines by upload and voucher ID, and aggregates repeated source lines
    before Python receives them. ``None`` means the additive layer is not
    populated yet, allowing the controlled legacy fallback during rollout.
    """
    if journal_upload_id is not None and journal_template_id is not None:
        raise ValueError("Specify journal_upload_id or journal_template_id, not both")
    if period_start is None or period_end is None:
        return None

    selected_upload_ids: list[int]
    source_identity: CashflowReportSourceIdentity | None = None
    source_identity_seconds = 0.0
    classification_query_seconds = 0.0
    if journal_upload_id is not None:
        selected_upload_ids = [int(journal_upload_id)]
        source_key = _cashflow_upload_ids_key(selected_upload_ids)
        source_mode = "explicit_upload"
        suppressed_upload_count = 0
    elif journal_template_id is not None:
        source_identity_started = perf_counter()
        source_identity = select_cashflow_report_source_identity(client_id, journal_template_id, query_fn)
        source_identity_seconds = perf_counter() - source_identity_started
        selected_upload_ids = list(source_identity.upload_ids)
        source_key = source_identity.source_key
        source_mode = source_identity.selection_mode
        suppressed_upload_count = source_identity.duplicate_uploads_suppressed
    else:
        return None
    if not selected_upload_ids:
        return None

    if journal_template_id is not None and journal_upload_id is None:
        if source_identity and source_identity.selection_mode == "canonical_register":
            source_cte = canonical_cashflow_report_voucher_selection_cte()
            source_cte_separator = ","
            source_join = """
                JOIN selected_report_vouchers selected_report_voucher
                  ON selected_report_voucher.upload_id = c.upload_id
                 AND selected_report_voucher.voucher_identity = UPPER(REGEXP_REPLACE(COALESCE(c.voucher_id, ''), '\\s+', '', 'g'))
            """
            source_filter = ""
            source_params = [client_id, journal_template_id, client_id, journal_template_id]
        else:
            source_cte = cashflow_report_voucher_selection_cte()
            source_cte_separator = ","
            source_join = """
                JOIN selected_report_vouchers selected_report_voucher
                  ON selected_report_voucher.upload_id = c.upload_id
                 AND selected_report_voucher.voucher_identity = UPPER(REGEXP_REPLACE(COALESCE(c.voucher_id, ''), '\\s+', '', 'g'))
            """
            source_filter = ""
            source_params = [
                client_id, journal_template_id,
                client_id, client_id, journal_template_id,
                client_id, journal_template_id,
            ]
    else:
        source_cte = "WITH"
        source_cte_separator = ""
        source_join = ""
        source_filter = "AND c.upload_id = ANY(%s)"
        source_params = [selected_upload_ids]
    query = f"""
        {source_cte}
        {source_cte_separator} classified_lines AS (
            SELECT
                c.client_data_id,
                c.upload_id,
                c.voucher_id,
                c.transaction_date AS date,
                c.account_code,
                -- New derived rows carry the label; the JSONB fallback keeps older rows compatible.
                COALESCE(
                    NULLIF(BTRIM(c.account_name), ''),
                    NULLIF(BTRIM(cd.row_data ->> 'Account Name'), ''),
                    NULLIF(BTRIM(cd.row_data ->> 'Account Title'), ''),
                    NULLIF(BTRIM(cd.row_data ->> 'Ledger Name'), ''),
                    NULLIF(BTRIM(cd.row_data ->> 'Particulars'), ''),
                    ''
                ) AS account_name,
                c.transaction_month,
                c.transaction_fortnight,
                COALESCE(
                    cd.row_data ->> 'Voucher Detail',
                    cd.row_data ->> 'Detail',
                    cd.row_data ->> 'Description',
                    cd.row_data ->> 'Narration',
                    cd.row_data ->> 'Remarks',
                    ''
                ) AS voucher_detail,
                c.debit,
                c.credit,
                c.treasury_type,
                c.classification_status,
                COALESCE(category.label, '') AS category
            FROM client_data cd
            JOIN cash_bank_transaction_classifications c
              ON cd.id = c.client_data_id
             AND cd.is_deleted = FALSE
            {source_join}
            LEFT JOIN account_control_cashflow_category_assignments category_assignment
              ON category_assignment.client_id = c.client_id
             AND category_assignment.account_code = c.account_code
            LEFT JOIN account_control_cashflow_categories category
              ON category.id = category_assignment.category_id
             AND category.client_id = c.client_id
             AND category.is_active = TRUE
            WHERE c.client_id = %s
              {source_filter}
              AND c.is_current = TRUE
              AND c.transaction_date >= %s
              AND c.transaction_date <= %s
              AND c.voucher_id IS NOT NULL
              AND c.account_code IS NOT NULL
              AND (c.debit <> 0 OR c.credit <> 0)
        ), selected_vouchers AS (
            SELECT DISTINCT upload_id, voucher_id
            FROM classified_lines
            WHERE treasury_type IN ('cash', 'bank')
        )
        SELECT
            l.voucher_id,
            l.date,
            l.account_code,
            MAX(l.account_name) AS account_name,
            MAX(l.voucher_detail) AS voucher_detail,
            SUM(l.debit) AS debit,
            SUM(l.credit) AS credit,
            MAX(l.treasury_type) AS treasury_type,
            MAX(l.category) AS category,
            MAX(l.transaction_month) AS transaction_month,
            MAX(l.transaction_fortnight) AS transaction_fortnight,
            COUNT(*) AS source_line_count,
            MAX(l.classification_status) AS classification_status
        FROM classified_lines l
        JOIN selected_vouchers v
          ON v.upload_id = l.upload_id
         AND v.voucher_id = l.voucher_id
        GROUP BY l.upload_id, l.voucher_id, l.date, l.account_code
        ORDER BY l.date, l.voucher_id, l.account_code
    """
    classification_query_started = perf_counter()
    rows = query_fn(
        query,
        tuple([*source_params, client_id, period_start, period_end]),
    )
    classification_query_seconds = perf_counter() - classification_query_started
    if not rows or not all("classification_status" in row for row in rows):
        return None

    source_transform_started = perf_counter()
    records = [dict(row) for row in rows]
    journal = pd.DataFrame(records).reindex(columns=[*JOURNAL_COLUMNS, *CASHFLOW_PREPARED_PERIOD_COLUMNS])
    if journal.empty:
        return None
    journal["date"] = pd.to_datetime(journal["date"], errors="coerce")
    for column in ("debit", "credit"):
        journal[column] = pd.to_numeric(journal[column], errors="coerce").fillna(0.0)
    journal["voucher_id"] = journal["voucher_id"].map(_text)
    journal["account_code"] = journal["account_code"].map(_canonical_code)
    for column in ("account_name", "voucher_detail"):
        journal[column] = journal[column].map(_text)
    journal = journal.loc[
        journal["voucher_id"].ne("")
        & journal["account_code"].ne("")
        & journal["date"].notna()
    ].reset_index(drop=True)
    if journal.empty:
        return None

    coa = pd.DataFrame(
        [
            {
                "account_code": row.get("account_code"),
                "account_title": row.get("account_name") or "",
                "account_type": row.get("treasury_type") or "",
                "category": row.get("category") or "",
            }
            for row in records
        ]
    ).drop_duplicates("account_code", keep="last")
    metadata = {
        "client_id": client_id,
        "journal_template_id": journal_template_id,
        "source_template_ids": [journal_template_id] if journal_template_id is not None else [],
        "source_upload_ids": selected_upload_ids,
        "source_row_count": int(sum(int(row.get("source_line_count") or 0) for row in records)),
        "valid_row_count": int(sum(int(row.get("source_line_count") or 0) for row in records)),
        "rejected_row_count": 0,
        "layout_row_count": 0,
        "duplicate_upload_suppression": source_mode == "latest_voucher",
        "duplicate_uploads_suppressed": suppressed_upload_count,
        "selected_template_upload_ids": selected_upload_ids,
        "cashflow_report_source_key": source_key,
        "cashflow_report_source_mode": source_mode,
        "source_upload_selection": "latest complete voucher occurrence by upload date and ID",
        "canonical_source_pending_activation": False,
        "date_prefilter_applied": True,
        "source_period_start": str(period_start),
        "source_period_end": str(period_end),
        "treasury_filter_stage": "sql_derived_classification",
        "sql_aggregated": True,
        "raw_json_parsing": False,
        "prepared_periods_requested": str(cashflow_group_by or "").casefold() in {"month", "fortnight"},
        "cashflow_source_identity_seconds": source_identity_seconds,
        "cashflow_classification_query_seconds": classification_query_seconds,
        "cashflow_source_transform_seconds": perf_counter() - source_transform_started,
    }
    return LedgerReportSource(journal, coa, (), metadata, ())
def load_ledger_report_source(
    client_id: int,
    *,
    journal_upload_id: int | None = None,
    journal_template_id: int | None = None,
    period_start: Any = None,
    period_end: Any = None,
    query_fn: Callable[..., list[dict[str, Any]]] = execute_query_pg,
    account_category_source: str = "ledger_summary",
    treasury_account_codes: Sequence[str] | None = None,
    cashflow_group_by: str | None = None,
) -> LedgerReportSource:
    """Load one explicit Client Data Bank dataset and its client categories."""

    if account_category_source == "cashflow":
        prepared = load_cashflow_prepared_line_source(
            client_id,
            journal_template_id=journal_template_id,
            period_start=period_start,
            period_end=period_end,
            query_fn=query_fn,
        )
        if prepared is not None:
            return prepared
        optimized = load_cashflow_sql_source(
            client_id,
            journal_upload_id=journal_upload_id,
            journal_template_id=journal_template_id,
            period_start=period_start,
            period_end=period_end,
            cashflow_group_by=cashflow_group_by,
            query_fn=query_fn,
        )
        if optimized is not None:
            return optimized

    if journal_upload_id is not None and journal_template_id is not None:
        raise ValueError("Specify journal_upload_id or journal_template_id, not both")

    filters = ["cd.client_id = %s", "cd.is_deleted = FALSE"]
    params: list[Any] = [client_id]
    metadata: dict[str, Any] = {"client_id": client_id}
    if journal_upload_id is not None:
        filters.append("cd.upload_id = %s")
        params.append(journal_upload_id)
        metadata["journal_upload_id"] = journal_upload_id
    elif journal_template_id is not None:
        filters.append("cd.template_id = %s")
        params.append(journal_template_id)
        metadata["journal_template_id"] = journal_template_id
    else:
        metadata["selection_required"] = True

    prefilter_mapping: dict[str, str] = {}
    if account_category_source == "cashflow" and journal_template_id is not None:
        prefilter_mapping = _load_cashflow_column_mapping(client_id, journal_template_id, query_fn)
        metadata["cashflow_mapping_template_id"] = journal_template_id
        metadata["cashflow_column_mapping"] = dict(prefilter_mapping)
        metadata["cashflow_mapping_ready"] = all(role in prefilter_mapping for role in CASHFLOW_REQUIRED_COLUMN_ROLES)

    if period_start is not None:
        filters.append("cd.extract_date >= %s")
        params.append(period_start)
    if period_end is not None:
        filters.append("cd.extract_date <= %s")
        params.append(period_end)
    metadata["date_prefilter_applied"] = period_start is not None or period_end is not None
    metadata["source_period_start"] = str(period_start) if period_start is not None else None
    metadata["source_period_end"] = str(period_end) if period_end is not None else None

    suppress_duplicate_uploads = journal_template_id is not None and journal_upload_id is None
    selected_upload_ids: list[int] = []
    suppressed_upload_count = 0
    if suppress_duplicate_uploads:
        selected_upload_ids, suppressed_upload_count = select_latest_template_upload_ids(client_id, journal_template_id, query_fn)
        if selected_upload_ids:
            filters.append("cd.upload_id = ANY(%s)")
            params.append(selected_upload_ids)
    metadata["duplicate_upload_suppression"] = suppress_duplicate_uploads
    metadata["duplicate_uploads_suppressed"] = suppressed_upload_count
    metadata["selected_template_upload_ids"] = selected_upload_ids

    treasury_codes = sorted({_canonical_code(code) for code in (treasury_account_codes or []) if _canonical_code(code)})
    metadata["treasury_prefilter_applied"] = False
    metadata["treasury_filter_stage"] = "cashflow_summary" if treasury_codes else None
    metadata["treasury_prefilter_account_count"] = len(treasury_codes)
    query_params: list[Any] = list(params)
    where_sql = " AND ".join(filters)
    projection = """
        SELECT cd.id AS client_data_id, cd.template_id, cd.upload_id,
               cd.row_data, cd.extract_date
    """
    query = f"""
    {projection}
    FROM client_data cd
    WHERE {where_sql}
    ORDER BY cd.extract_date NULLS LAST, cd.id
    """

    rows = query_fn(query, tuple(query_params))
    if not rows:
        metadata["source_row_count"] = 0
        source = build_report_source([], [], period_start=period_start, period_end=period_end, metadata=metadata)
        return source

    template_ids = {row.get("template_id") for row in rows if row.get("template_id") is not None}
    upload_ids = {row.get("upload_id") for row in rows if row.get("upload_id") is not None}
    if journal_upload_id is None and journal_template_id is None and (len(template_ids) > 1 or len(upload_ids) > 1):
        metadata["selection_required"] = True
        metadata["template_ids"] = sorted(template_ids)
        metadata["upload_ids"] = sorted(upload_ids)
    elif journal_upload_id is None and journal_template_id is None:
        metadata["selection_required"] = False

    metadata["source_row_count"] = len(rows)
    metadata["source_template_ids"] = sorted(template_ids)
    metadata["source_upload_ids"] = sorted(upload_ids)
    column_mapping: dict[str, str] = dict(prefilter_mapping)
    mapping_template_id = journal_template_id if journal_template_id is not None else (next(iter(template_ids)) if len(template_ids) == 1 else None)
    if account_category_source == "cashflow" and mapping_template_id is not None and not column_mapping:
        column_mapping = _load_cashflow_column_mapping(client_id, mapping_template_id, query_fn)
        metadata["cashflow_mapping_template_id"] = mapping_template_id
        metadata["cashflow_column_mapping"] = dict(column_mapping)
        metadata["cashflow_mapping_ready"] = all(role in column_mapping for role in CASHFLOW_REQUIRED_COLUMN_ROLES)
    account_diagnostics: list[dict[str, Any]] = []
    account_rows = _load_account_rows(client_id, query_fn, account_diagnostics, category_source=account_category_source)
    source = build_report_source(
        rows,
        account_rows,
        period_start=period_start,
        period_end=period_end,
        column_mapping=column_mapping or None,
        metadata=metadata,
    )
    if account_diagnostics:
        source = LedgerReportSource(
            source.journal,
            source.coa,
            tuple([*source.diagnostics, *account_diagnostics]),
            source.metadata,
            source.rejected_rows,
        )
    if account_category_source == "cashflow" and not metadata.get("selection_required"):
        mapping_diagnostics = list(source.diagnostics)
        missing_roles = [role for role in CASHFLOW_REQUIRED_COLUMN_ROLES if role not in column_mapping]
        if mapping_template_id is None:
            mapping_diagnostics.append(
                _diagnostic("cashflow_column_mapping_required", "error", "Select and map a Transaction template before generating Cashflows.")
            )
        elif missing_roles:
            mapping_diagnostics.append(
                _diagnostic(
                    "cashflow_column_mapping_incomplete",
                    "Required Cashflows template columns are not mapped: " + ", ".join(missing_roles) + ".",
                    fields=missing_roles,
                    template_id=mapping_template_id,
                )
            )
        if len(mapping_diagnostics) != len(source.diagnostics):
            source = LedgerReportSource(source.journal, source.coa, tuple(mapping_diagnostics), source.metadata, source.rejected_rows)
    if metadata.get("selection_required"):
        diagnostics = list(source.diagnostics)
        diagnostics.append(
            _diagnostic(
                "ambiguous_journal_source",
                "error",
                "More than one Client Data Bank dataset matches this client; select one upload or template before generating a report.",
                template_ids=metadata.get("template_ids", []),
                upload_ids=metadata.get("upload_ids", []),
            )
        )
        return LedgerReportSource(source.journal, source.coa, tuple(diagnostics), source.metadata, source.rejected_rows)
    return source
