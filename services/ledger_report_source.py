"""Application data adapter for the ledger reconciliation engine.

This module translates stored Client Data Bank rows and report-scoped Trial
Balance account assignments into the canonical DataFrames consumed by
``services.ledger_group_summary``.  It has no report calculations and never
modifies application data.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
import json
import re
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
CASHFLOW_CHILD_DETAILS_VERSION = 2

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


def _latest_template_upload_ids(
    client_id: int,
    template_id: int,
    query_fn: Callable[..., list[dict[str, Any]]],
) -> tuple[list[int], int]:
    """Select the newest upload for each filename without reading row_data."""

    rows = query_fn(
        """
        SELECT f.id AS upload_id, f.file_name, f.upload_date
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
        ORDER BY f.upload_date DESC NULLS LAST, f.id DESC
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


def _cashflow_summary_payload(summary: Mapping[str, Any]) -> dict[str, Any]:
    periods = [dict(period) for period in summary.get("periods", [])]
    period_keys = [str(period["key"]) for period in periods]
    child_details_prepared = all(
        key in summary
        for key in ("transfer_details", "deduction_details", "categories_needing_review")
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
    return {
        "months": [str(month) for month in summary.get("months", [])],
        "periods": periods,
        "categories": categories,
        "source_line_count": int(summary.get("source_line_count", 0) or 0),
        "selected_voucher_count": int(summary.get("selected_voucher_count", 0) or 0),
        "internal_transfer_count": int(summary.get("internal_transfer_count", 0) or 0),
        "category_review_count": int(summary.get("category_review_count", 0) or 0),
        "child_details_prepared": child_details_prepared,
        "child_details_version": CASHFLOW_CHILD_DETAILS_VERSION if child_details_prepared else 0,
        "transfer_details": _cashflow_detail_records(summary.get("transfer_details")),
        "deduction_details": _cashflow_detail_records(summary.get("deduction_details")),
        "categories_needing_review": _cashflow_detail_records(summary.get("categories_needing_review")),
        "period_metrics": summary.get("period_metrics") or {},
        "receipt_presentation": summary.get("receipt_presentation") or "net",
    }


def _cashflow_summary_from_payload(payload: Mapping[str, Any], group_by: str) -> dict[str, Any]:
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
    child_details_version = int(payload.get("child_details_version", 0) or 0)
    child_details_prepared = bool(payload.get("child_details_prepared")) and child_details_version >= CASHFLOW_CHILD_DETAILS_VERSION
    return {
        "months": [str(month) for month in payload.get("months", [])],
        "periods": periods,
        "group_by": group_by,
        "category_totals": category_totals,
        "transfer_details": _cashflow_detail_frame(payload, "transfer_details") if child_details_prepared else pd.DataFrame(),
        "deduction_details": _cashflow_detail_frame(payload, "deduction_details") if child_details_prepared else pd.DataFrame(),
        "categories_needing_review": _cashflow_detail_frame(payload, "categories_needing_review") if child_details_prepared else pd.DataFrame(),
        "selected_vouchers": [],
        "selected_voucher_count": int(payload.get("selected_voucher_count", 0) or 0),
        "internal_transfer_count": int(payload.get("internal_transfer_count", 0) or 0),
        "category_review_count": int(payload.get("category_review_count", 0) or 0),
        "source_line_count": int(payload.get("source_line_count", 0) or 0),
        "period_metrics": payload.get("period_metrics") or {},
        "receipt_presentation": payload.get("receipt_presentation") or "net",
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
            ), '{}'::json) AS settings
        """,
        (client_id, client_id),
    )
    row = rows[0] if rows else {}
    treasury = _cashflow_json_value(row.get("treasury"))
    settings = _cashflow_json_value(row.get("settings"))
    treasury = treasury if isinstance(treasury, list) else []
    settings = settings if isinstance(settings, Mapping) else {}
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
) -> dict[str, Any]:
    from services.ledger_group_summary import _cashflow_periods, normalize_cashflow_grouping

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
        totals = {"source_line_count": 0, "selected_voucher_count": 0, "internal_transfer_count": 0, "category_review_count": 0}
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
    totals = {
        metric: sum(values[metric] for values in period_metrics.values())
        for metric in ("source_line_count", "selected_voucher_count", "internal_transfer_count", "category_review_count")
    }
    child_details_version = int(payload.get("child_details_version", 0) or 0)
    child_details_prepared = bool(payload.get("child_details_prepared")) and child_details_version >= CASHFLOW_CHILD_DETAILS_VERSION
    transfer_details = _cashflow_rollup_detail_frame(payload, "transfer_details", periods) if child_details_prepared else pd.DataFrame()
    deduction_details = _cashflow_rollup_detail_frame(payload, "deduction_details", periods) if child_details_prepared else pd.DataFrame()
    review = _cashflow_rollup_detail_frame(payload, "categories_needing_review", periods) if child_details_prepared else pd.DataFrame()
    if child_details_prepared:
        totals["internal_transfer_count"] = len(transfer_details)
        totals["category_review_count"] = len(review)
    months = [
        str(period)
        for period in pd.period_range(pd.Timestamp(period_start).to_period("M"), pd.Timestamp(period_end).to_period("M"), freq="M")
    ]
    return {
        "months": months,
        "periods": periods,
        "group_by": grouping,
        "category_totals": category_totals,
        "transfer_details": transfer_details,
        "deduction_details": deduction_details,
        "categories_needing_review": review,
        "selected_vouchers": [],
        "source_line_count": totals["source_line_count"],
        "selected_voucher_count": totals["selected_voucher_count"],
        "internal_transfer_count": totals["internal_transfer_count"],
        "category_review_count": totals["category_review_count"],
        "period_metrics": period_metrics,
        "receipt_presentation": payload.get("receipt_presentation") or "net",
        "child_details_prepared": child_details_prepared,
        "child_details_version": child_details_version,
    }


def load_cashflow_prepared_summary(
    client_id: int,
    *,
    journal_template_id: int | None,
    period_start: Any,
    period_end: Any,
    group_by: str,
    receipt_presentation: str,
    query_fn: Callable[..., list[dict[str, Any]]] = execute_query_pg,
) -> dict[str, Any] | None:
    """Roll a full-range daily snapshot into the requested report periods."""
    if journal_template_id is None:
        return None
    upload_ids, suppressed_count = _latest_template_upload_ids(client_id, journal_template_id, query_fn)
    if not upload_ids:
        return None
    bounds = _cashflow_snapshot_bounds(client_id, journal_template_id, upload_ids, query_fn)
    if bounds is None:
        return None
    source_start, source_end = bounds
    rows = query_fn(
        """
        SELECT summary_payload, configuration_version, created_at
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
        """,
        (client_id, journal_template_id, _cashflow_upload_ids_key(upload_ids), source_start, source_end, receipt_presentation),
    )
    if not rows:
        return None
    payload = _cashflow_json_value(rows[0].get("summary_payload"))
    if not isinstance(payload, Mapping):
        return None
    summary = _cashflow_daily_rollup(payload, period_start, period_end, group_by)
    metadata = {
        "client_id": client_id,
        "journal_template_id": journal_template_id,
        "source_template_ids": [journal_template_id],
        "source_upload_ids": upload_ids,
        "selected_template_upload_ids": upload_ids,
        "duplicate_upload_suppression": True,
        "duplicate_uploads_suppressed": suppressed_count,
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
        "child_details_prepared": bool(summary.get("child_details_prepared")),
        "child_details_version": int(summary.get("child_details_version", 0) or 0),
        "classification_configuration_version": rows[0].get("configuration_version"),
        "summary_read_model_created_at": rows[0].get("created_at"),
    }
    return {"summary": summary, "metadata": metadata}


def invalidate_cashflow_summary_read_models(
    client_id: int,
    *,
    write_fn: Callable[..., Any] = execute_write_pg,
) -> None:
    """Invalidate all prepared and exact Cashflows summaries for one client."""
    write_fn(
        "DELETE FROM cash_bank_summary_read_models WHERE client_id = %s",
        (client_id,),
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
    upload_ids, _suppressed_count = _latest_template_upload_ids(client_id, int(template_id), query_fn)
    if not upload_ids:
        return {"status": "skipped", "reason": "no_source_uploads"}
    bounds = _cashflow_snapshot_bounds(client_id, int(template_id), upload_ids, query_fn)
    if bounds is None:
        return {"status": "skipped", "reason": "no_classified_dates"}
    source_start, source_end = bounds
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
        group_by="day",
    )
    store_cashflow_summary_read_model(
        client_id,
        journal_template_id=int(template_id),
        source_upload_ids=source.metadata.get("selected_template_upload_ids") or upload_ids,
        configuration_version=source.metadata.get("classification_configuration_version") or 1,
        period_start=source_start,
        period_end=source_end,
        group_by="day",
        summary=summary,
        write_fn=write_fn,
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
    query_fn: Callable[..., list[dict[str, Any]]] = execute_query_pg,
) -> dict[str, Any] | None:
    """Load an exact summary-only snapshot when the source/configuration is unchanged."""
    if journal_template_id is None:
        return None
    upload_ids, suppressed_count = _latest_template_upload_ids(client_id, journal_template_id, query_fn)
    if not upload_ids:
        return None
    upload_key = _cashflow_upload_ids_key(upload_ids)
    rows = query_fn(
        """
        SELECT summary_payload, configuration_version, created_at
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
        """,
        (client_id, journal_template_id, upload_key, period_start, period_end, group_by, receipt_presentation),
    )
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
    summary = _cashflow_summary_from_payload(payload, group_by)
    metadata = {
        "client_id": client_id,
        "journal_template_id": journal_template_id,
        "source_template_ids": [journal_template_id],
        "source_upload_ids": upload_ids,
        "selected_template_upload_ids": upload_ids,
        "duplicate_upload_suppression": True,
        "duplicate_uploads_suppressed": suppressed_count,
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
        "child_details_prepared": bool(payload.get("child_details_prepared")),
        "child_details_version": int(payload.get("child_details_version", 0) or 0),
        "classification_configuration_version": rows[0].get("configuration_version"),
        "summary_read_model_created_at": rows[0].get("created_at"),
    }
    return {"summary": summary, "metadata": metadata}


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
    write_fn: Callable[..., Any] = execute_write_pg,
) -> None:
    """Persist one completed summary-only snapshot for safe repeat loads."""
    if journal_template_id is None or not source_upload_ids:
        return
    payload = _cashflow_summary_payload(summary)
    write_fn(
        """
        INSERT INTO cash_bank_summary_read_models (
            client_id, template_id, source_upload_ids_key, configuration_version,
            period_start, period_end, group_by, receipt_presentation, summary_payload
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
        ON CONFLICT (client_id, template_id, source_upload_ids_key, period_start, period_end, group_by, receipt_presentation)
        DO UPDATE SET
            configuration_version = EXCLUDED.configuration_version,
            summary_payload = EXCLUDED.summary_payload,
            created_at = CURRENT_TIMESTAMP
        """,
        (
            client_id,
            journal_template_id,
            _cashflow_upload_ids_key(source_upload_ids),
            int(configuration_version or 1),
            period_start,
            period_end,
            group_by,
            payload["receipt_presentation"],
            json.dumps(payload),
        ),
    )

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
    counter-account lines by voucher ID, and aggregates repeated source lines
    before Python receives them. ``None`` means the additive layer is not
    populated yet, allowing the controlled legacy fallback during rollout.
    """
    if journal_upload_id is not None and journal_template_id is not None:
        raise ValueError("Specify journal_upload_id or journal_template_id, not both")
    if period_start is None or period_end is None:
        return None

    selected_upload_ids: list[int]
    suppressed_upload_count = 0
    if journal_upload_id is not None:
        selected_upload_ids = [int(journal_upload_id)]
    elif journal_template_id is not None:
        selected_upload_ids, suppressed_upload_count = _latest_template_upload_ids(
            client_id, journal_template_id, query_fn
        )
    else:
        return None
    if not selected_upload_ids:
        return None

    query = """
        WITH classified_lines AS (
            SELECT
                c.client_data_id,
                c.voucher_id,
                c.transaction_date AS date,
                c.account_code,
                c.transaction_month,
                c.transaction_fortnight,
                COALESCE(
                    cd.row_data ->> 'Account Name',
                    cd.row_data ->> 'Account Title',
                    cd.row_data ->> 'Ledger Name',
                    cd.row_data ->> 'Particulars',
                    ''
                ) AS account_name,
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
            LEFT JOIN account_control_cashflow_category_assignments category_assignment
              ON category_assignment.client_id = c.client_id
             AND category_assignment.account_code = c.account_code
            LEFT JOIN account_control_cashflow_categories category
              ON category.id = category_assignment.category_id
             AND category.client_id = c.client_id
             AND category.is_active = TRUE
            WHERE c.client_id = %s
              AND c.template_id = %s
              AND c.upload_id = ANY(%s)
              AND c.is_current = TRUE
              AND c.transaction_date >= %s
              AND c.transaction_date <= %s
              AND c.voucher_id IS NOT NULL
              AND c.account_code IS NOT NULL
              AND (c.debit <> 0 OR c.credit <> 0)
        ), selected_vouchers AS (
            SELECT DISTINCT voucher_id
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
        JOIN selected_vouchers v ON v.voucher_id = l.voucher_id
        GROUP BY l.voucher_id, l.date, l.account_code
        ORDER BY l.date, l.voucher_id, l.account_code
    """
    rows = query_fn(
        query,
        (client_id, journal_template_id, selected_upload_ids, period_start, period_end),
    )
    if not rows or not all("classification_status" in row for row in rows):
        return None

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
        "duplicate_upload_suppression": journal_template_id is not None and journal_upload_id is None,
        "duplicate_uploads_suppressed": suppressed_upload_count,
        "selected_template_upload_ids": selected_upload_ids,
        "date_prefilter_applied": True,
        "source_period_start": str(period_start),
        "source_period_end": str(period_end),
        "treasury_filter_stage": "sql_derived_classification",
        "sql_aggregated": True,
        "raw_json_parsing": False,
        "prepared_periods_requested": str(cashflow_group_by or "").casefold() in {"month", "fortnight"},
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
        selected_upload_ids, suppressed_upload_count = _latest_template_upload_ids(client_id, journal_template_id, query_fn)
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
