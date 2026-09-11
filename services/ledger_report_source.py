"""Application data adapter for the ledger reconciliation engine.

This module translates stored Client Data Bank rows and report-scoped Trial
Balance account assignments into the canonical DataFrames consumed by
``services.ledger_group_summary``.  It has no report calculations and never
modifies application data.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import re
from typing import Any, Callable, Mapping, Sequence

import pandas as pd

from database import execute_query_pg


JOURNAL_COLUMNS = (
    "voucher_id",
    "date",
    "account_code",
    "account_name",
    "voucher_detail",
    "debit",
    "credit",
)
COA_COLUMNS = ("account_code", "account_title", "account_type", "category")

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
_COA_ALIASES: Mapping[str, tuple[str, ...]] = {
    "account_code": ("account_code", "account code", "account #", "account no", "code"),
    "account_title": ("account_title", "account title", "account name", "title", "name"),
    "account_type": ("account_type", "account type", "source account type", "type"),
    "category": ("category", "category name", "cat", "assigned category"),
}


def _normalise_label(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").strip().casefold())


def _canonical_code(value: Any) -> str:
    if value is None or pd.isna(value):
        return ""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        numeric = float(value)
        if numeric.is_integer():
            return str(int(numeric))
    return str(value).strip()


def _text(value: Any) -> str:
    if value is None or pd.isna(value):
        return ""
    return str(value).strip()


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
) -> dict[str, Any]:
    by_label: dict[str, list[Any]] = {}
    for column in columns:
        by_label.setdefault(_normalise_label(column), []).append(column)

    resolved: dict[str, Any] = {}
    for canonical, candidates in aliases.items():
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
    """Canonical report inputs plus diagnostics produced during adaptation."""

    journal: pd.DataFrame
    coa: pd.DataFrame
    diagnostics: tuple[dict[str, Any], ...]
    metadata: Mapping[str, Any]

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
    metadata: Mapping[str, Any] | None = None,
) -> LedgerReportSource:
    """Normalize stored client rows and account assignments for Phase 1.

    ``client_data_rows`` may be the direct JSON row dictionaries used by tests
    or database-shaped rows containing ``row_data``.  The source layout is
    resolved from explicit field aliases, never from account names or voucher
    prefixes.
    """

    diagnostics: list[dict[str, Any]] = []
    raw_rows = client_data_rows.to_dict("records") if isinstance(client_data_rows, pd.DataFrame) else list(client_data_rows)
    records: list[dict[str, Any]] = []
    invalid_json_count = 0
    for row in raw_rows:
        source = row.get("row_data") if isinstance(row, Mapping) and "row_data" in row else row
        decoded = _decode_row_data(source)
        if decoded is None:
            invalid_json_count += 1
            continue
        records.append(decoded)
    if invalid_json_count:
        diagnostics.append(
            _diagnostic(
                "invalid_client_data_row",
                "error",
                f"{invalid_json_count} stored Client Data row(s) do not contain a JSON object.",
                count=invalid_json_count,
            )
        )
    if not records:
        diagnostics.append(
            _diagnostic("no_journal_rows", "error", "No usable Client Data journal rows were found.")
        )
        return LedgerReportSource(_empty_journal(), _normalise_account_rows(account_rows, diagnostics), tuple(diagnostics), metadata or {})

    frame = pd.DataFrame(records)
    resolved = _resolve_columns(frame.columns, _JOURNAL_ALIASES, diagnostics)
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
        return LedgerReportSource(_empty_journal(), _normalise_account_rows(account_rows, diagnostics), tuple(diagnostics), metadata or {})

    journal = pd.DataFrame(index=frame.index)
    for canonical in JOURNAL_COLUMNS:
        source = resolved.get(canonical)
        journal[canonical] = frame[source] if source is not None else ""
    journal["voucher_id"] = journal["voucher_id"].map(_text)
    journal["account_code"] = journal["account_code"].map(_canonical_code)
    for column in ("account_name", "voucher_detail"):
        journal[column] = journal[column].map(_text)

    missing_value_mask = (
        journal["voucher_id"].eq("")
        | journal["account_code"].eq("")
        | journal["date"].map(_text).eq("")
    )
    if missing_value_mask.any():
        diagnostics.append(
            _diagnostic(
                "blank_journal_identity",
                "error",
                f"{int(missing_value_mask.sum())} journal row(s) have a blank voucher ID, date, or account code.",
                count=int(missing_value_mask.sum()),
            )
        )

    parsed_dates = pd.to_datetime(journal["date"], errors="coerce")
    invalid_dates = parsed_dates.isna() & journal["date"].map(_text).ne("")
    if invalid_dates.any():
        diagnostics.append(
            _diagnostic(
                "invalid_journal_dates",
                "error",
                f"{int(invalid_dates.sum())} journal row(s) have invalid dates.",
                count=int(invalid_dates.sum()),
            )
        )
    journal["date"] = parsed_dates

    for column in ("debit", "credit"):
        numeric = pd.to_numeric(journal[column], errors="coerce")
        invalid = numeric.isna() & journal[column].map(_text).ne("")
        if invalid.any():
            diagnostics.append(
                _diagnostic(
                    "invalid_journal_amounts",
                    "error",
                    f"{int(invalid.sum())} journal row(s) have non-numeric {column} values.",
                    field=column,
                    count=int(invalid.sum()),
                )
            )
        journal[column] = numeric.fillna(0.0).astype(float)

    if period_start is not None or period_end is not None:
        start = pd.Timestamp(period_start) if period_start is not None else journal["date"].min()
        end = pd.Timestamp(period_end) if period_end is not None else journal["date"].max()
        if pd.isna(start) or pd.isna(end) or start > end:
            diagnostics.append(_diagnostic("invalid_reporting_period", "error", "The reporting period is invalid."))
        else:
            journal = journal.loc[journal["date"].between(start, end, inclusive="both")].copy()

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
        metadata or {},
    )


def _load_account_rows(
    client_id: int,
    query_fn: Callable[..., list[dict[str, Any]]],
    diagnostics: list[dict[str, Any]],
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
    assignment_rows = []
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


def load_ledger_report_source(
    client_id: int,
    *,
    journal_upload_id: int | None = None,
    journal_template_id: int | None = None,
    period_start: Any = None,
    period_end: Any = None,
    query_fn: Callable[..., list[dict[str, Any]]] = execute_query_pg,
) -> LedgerReportSource:
    """Load one explicit Client Data Bank dataset and its client categories."""

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

    rows = query_fn(
        f"""
        SELECT cd.id AS client_data_id, cd.template_id, cd.upload_id,
               cd.row_data, cd.extract_date
        FROM client_data cd
        WHERE {' AND '.join(filters)}
        ORDER BY cd.extract_date NULLS LAST, cd.id
        """,
        tuple(params),
    )
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
    account_diagnostics: list[dict[str, Any]] = []
    account_rows = _load_account_rows(client_id, query_fn, account_diagnostics)
    source = build_report_source(
        rows,
        account_rows,
        period_start=period_start,
        period_end=period_end,
        metadata=metadata,
    )
    if account_diagnostics:
        source = LedgerReportSource(
            source.journal,
            source.coa,
            tuple([*source.diagnostics, *account_diagnostics]),
            source.metadata,
        )
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
        return LedgerReportSource(source.journal, source.coa, tuple(diagnostics), source.metadata)
    return source
