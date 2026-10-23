"""Backend configuration for ledger reconciliation account groups."""

from __future__ import annotations

from contextlib import nullcontext
from datetime import date, datetime
from decimal import Decimal
from io import BytesIO
import hashlib
import json
import logging
import math
import re
from time import perf_counter
from typing import Any, List, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
import pandas as pd
from pydantic import BaseModel, Field

from database import execute_query_pg, execute_write_pg, log_audit, pg_transaction, run_read_snapshot_pg
from routers.auth import require_admin
from services.ledger_group_summary import LedgerBalanceError, build_cashflow_summary, build_cross_group_check, build_financial_statement_summary, build_group_summary, normalize_cashflow_grouping
from services.ledger_report_source import (
    CASHFLOW_CHILD_DETAILS_VERSION,
    CASHFLOW_DRILLDOWN_DETAILS_VERSION,
    CASHFLOW_SUMMARY_PAYLOAD_VERSION,
    CASHFLOW_COLUMN_ROLE_LABELS,
    CASHFLOW_REQUIRED_COLUMN_ROLES,
    CASHFLOW_TREASURY_TOTALS_VERSION,
    LedgerReportSource,
    normalize_cashflow_treasury_view,
    project_cashflow_treasury_view,
    load_cashflow_summary_read_model,
    load_cashflow_prepared_summary,
    load_cashflow_prepared_rollup_summary,
    load_cashflow_prepared_drilldown,
    select_cashflow_report_source_identity,
    invalidate_cashflow_summary_read_models,
    load_ledger_report_source,
    load_cashflow_difference_source,
    load_cashflow_reconciliation_identity,
    cashflow_reconciliation_support_page,
    store_cashflow_summary_read_model,
)
from services.cashflow_preparation import (
    get_cashflow_preparation_status,
    retry_cashflow_preparation_job,
    run_cashflow_preparation_job_postgres,
)
from services.reconciliation_balance_source import (
    load_reconciliation_balance_source,
    load_uploaded_ledger_balances,
    build_cashflow_differences,
    build_cashflow_reconciliation,
    normalize_balance_month,
)
from services.reconciliation_xlsx import export_cashflow_xlsx, export_reconciliation_xlsx


router = APIRouter()
logger = logging.getLogger(__name__)

# Browser roll-ups are disposable; never persist another transaction/report copy.
CASHFLOW_BROWSER_LEDGER_MAX_ROWS = 5000
CASHFLOW_BROWSER_LEDGER_MAX_BYTES = 2 * 1024 * 1024


def _cashflow_configuration_key(configuration: dict[str, Any]) -> str:
    relevant = {key: configuration.get(key) for key in (
        'source_template_id', 'cash_account_codes', 'bank_account_codes',
        'match_internal_transfers', 'receipt_presentation', 'customer_payment_roles', 'category_mappings', 'vendor_account_codes',
    )}
    relevant['signed_party_totals_version'] = 1
    relevant['treasury_movement_proof_version'] = 1
    relevant['complete_deduction_details_version'] = 1
    return hashlib.sha256(json.dumps(relevant, sort_keys=True, default=str).encode('utf-8')).hexdigest()


def _cashflow_browser_ledger_payload(rows: list[dict[str, Any]], context: dict[str, Any]) -> dict[str, Any]:
    """All or nothing: oversized data must use the paginated endpoint."""
    payload = {'context': context, 'complete': False, 'row_count': len(rows), 'rows': []}
    if len(rows) > CASHFLOW_BROWSER_LEDGER_MAX_ROWS:
        return payload
    candidate = dict(payload, complete=True, rows=rows)
    size = len(json.dumps(candidate, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode('utf-8'))
    return candidate if size <= CASHFLOW_BROWSER_LEDGER_MAX_BYTES else payload


class AccountGroupUpdate(BaseModel):
    group_label: str
    account_codes: List[str] = Field(default_factory=list)


class ReconciliationBalanceInput(BaseModel):
    account_code: str
    period_month: str
    opening_balance: float
    actual_closing_balance: float


class ReconciliationBalanceInputsUpdate(BaseModel):
    group_label: str
    balances: List[ReconciliationBalanceInput] = Field(default_factory=list)


class ReportCategorySetCreate(BaseModel):
    purpose_key: str
    set_name: str


class ReportCategoryDefinition(BaseModel):
    category_key: str
    label: str
    parent_category_key: Optional[str] = None
    sort_order: int = 0


class ReportCategoryDefinitionsUpdate(BaseModel):
    categories: List[ReportCategoryDefinition] = Field(default_factory=list)


class ReportAccountCategoryAssignment(BaseModel):
    account_code: str
    category_key: str


class ReportCategoryAssignmentsUpdate(BaseModel):
    assignments: List[ReportAccountCategoryAssignment] = Field(default_factory=list)


def normalize_group_label(value: Any) -> str:
    """Trim and collapse whitespace while preserving the user's display casing."""
    return re.sub(r"\s+", " ", str(value or "").strip())


class CashflowColumnMappingEntry(BaseModel):
    role: str
    template_column_id: int


class CashflowTemplateMappingUpdate(BaseModel):
    client_id: int
    template_id: int
    mappings: List[CashflowColumnMappingEntry] = Field(default_factory=list)
    set_as_default: bool = True


class CashflowSourceSelectionUpdate(BaseModel):
    client_id: int
    upload_id: Optional[int] = Field(default=None, ge=1)
    template_id: Optional[int] = Field(default=None, ge=1)


def normalize_account_code(value: Any) -> str:
    """Normalize transport whitespace without changing an accounting code's case."""
    return str(value or "").strip()


def normalize_report_key(value: Any) -> str:
    """Normalize a report/category key to a stable lower-case identifier."""
    return re.sub(r"[^a-z0-9]+", "_", str(value or "").strip().casefold()).strip("_")


def _clean_report_category_set_request(payload: ReportCategorySetCreate) -> tuple[str, str]:
    purpose_key = normalize_report_key(payload.purpose_key)
    set_name = normalize_group_label(payload.set_name)
    if not purpose_key:
        raise HTTPException(400, "Purpose key is required")
    if len(purpose_key) > 80:
        raise HTTPException(400, "Purpose key must be 80 characters or fewer")
    if not set_name:
        raise HTTPException(400, "Category set name is required")
    if len(set_name) > 120:
        raise HTTPException(400, "Category set name must be 120 characters or fewer")
    return purpose_key, set_name


def _get_report_category_set(category_set_id: int) -> dict[str, Any]:
    rows = execute_query_pg(
        """
        SELECT id, client_id, purpose_key, set_name, is_active
        FROM report_category_sets
        WHERE id = %s AND is_active = TRUE
        """,
        (category_set_id,),
    )
    if not rows:
        raise HTTPException(404, "Report category set not found or inactive")
    return rows[0]


def _clean_report_categories(payload: ReportCategoryDefinitionsUpdate) -> list[dict[str, Any]]:
    cleaned: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in payload.categories:
        category_key = normalize_report_key(item.category_key)
        label = normalize_group_label(item.label)
        parent_key = normalize_report_key(item.parent_category_key) if item.parent_category_key else None
        if not category_key:
            raise HTTPException(400, "Category key is required")
        if len(category_key) > 80:
            raise HTTPException(400, "Category key must be 80 characters or fewer")
        if not label:
            raise HTTPException(400, "Category label is required")
        if len(label) > 120:
            raise HTTPException(400, "Category label must be 120 characters or fewer")
        if category_key in seen:
            raise HTTPException(400, f"Category key '{category_key}' is listed more than once")
        if parent_key == category_key:
            raise HTTPException(400, "A category cannot be its own parent")
        if item.sort_order < 0:
            raise HTTPException(400, "Category sort order cannot be negative")
        seen.add(category_key)
        cleaned.append(
            {
                "category_key": category_key,
                "label": label,
                "parent_category_key": parent_key,
                "sort_order": item.sort_order,
            }
        )
    keys = {item["category_key"] for item in cleaned}
    missing_parents = sorted(
        {item["parent_category_key"] for item in cleaned if item["parent_category_key"] and item["parent_category_key"] not in keys}
    )
    if missing_parents:
        raise HTTPException(400, detail={"message": "Parent category does not exist in this replacement", "category_keys": missing_parents})
    return cleaned


def _clean_report_assignments(payload: ReportCategoryAssignmentsUpdate) -> list[dict[str, str]]:
    cleaned: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in payload.assignments:
        account_code = normalize_account_code(item.account_code)
        category_key = normalize_report_key(item.category_key)
        if not account_code:
            raise HTTPException(400, "Account codes cannot be blank")
        if not category_key:
            raise HTTPException(400, "Category key is required")
        if account_code in seen:
            raise HTTPException(400, f"Account code '{account_code}' is listed more than once")
        seen.add(account_code)
        cleaned.append({"account_code": account_code, "category_key": category_key})
    return cleaned


def _clean_group_request(payload: AccountGroupUpdate) -> tuple[str, list[str]]:
    group_label = normalize_group_label(payload.group_label)
    if not group_label:
        raise HTTPException(400, "Group label is required")
    if len(group_label) > 100:
        raise HTTPException(400, "Group label must be 100 characters or fewer")

    account_codes: list[str] = []
    seen: set[str] = set()
    for raw_code in payload.account_codes:
        code = normalize_account_code(raw_code)
        if not code:
            raise HTTPException(400, "Account codes cannot be blank")
        if code in seen:
            raise HTTPException(400, f"Account code '{code}' is listed more than once")
        seen.add(code)
        account_codes.append(code)
    return group_label, account_codes


def _clean_balance_request(payload: ReconciliationBalanceInputsUpdate) -> tuple[str, list[dict[str, Any]]]:
    group_label = normalize_group_label(payload.group_label)
    if not group_label:
        raise HTTPException(400, "Group label is required")
    if len(group_label) > 100:
        raise HTTPException(400, "Group label must be 100 characters or fewer")

    cleaned: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for item in payload.balances:
        account_code = normalize_account_code(item.account_code)
        if not account_code:
            raise HTTPException(400, "Balance account codes cannot be blank")
        try:
            period_month = normalize_balance_month(item.period_month)
        except ValueError as error:
            raise HTTPException(400, str(error)) from error
        key = (account_code, period_month)
        if key in seen:
            raise HTTPException(400, f"Duplicate balance input for {account_code} in {period_month}")
        seen.add(key)
        if not math.isfinite(item.opening_balance) or not math.isfinite(item.actual_closing_balance):
            raise HTTPException(400, "Balance amounts must be finite numbers")
        cleaned.append(
            {
                "account_code": account_code,
                "period_month": period_month,
                "opening_balance": item.opening_balance,
                "actual_closing_balance": item.actual_closing_balance,
            }
        )
    return group_label, cleaned


def _require_active_client(client_id: int) -> None:
    rows = execute_query_pg(
        "SELECT id FROM clients WHERE id = %s AND is_active = TRUE",
        (client_id,),
    )
    if not rows:
        raise HTTPException(404, "Client not found or inactive")


def _latest_trial_balance_accounts(client_id: int) -> list[dict[str, Any]]:
    return execute_query_pg(
        """
        SELECT a.id, a.account_code, a.account_name, a.source_account_type,
               a.classification, u.period_start, u.period_end, u.period_type
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


def _suggested_group(account_type: Any) -> str | None:
    value = str(account_type or "").strip().casefold()
    if value == "bank":
        return "Bank"
    if value == "cash":
        return "Cash"
    return None


def _saved_codes(client_id: int, group_label: Optional[str] = None) -> set[str]:
    filters = ["client_id = %s", "is_active = TRUE"]
    params: list[Any] = [client_id]
    if group_label:
        filters.append("LOWER(group_label) = LOWER(%s)")
        params.append(normalize_group_label(group_label))
    rows = execute_query_pg(
        "SELECT account_code FROM account_groups WHERE " + " AND ".join(filters),
        tuple(params),
    )
    return {normalize_account_code(row["account_code"]) for row in rows}


def _stored_group_label(client_id: int, requested_label: str) -> str:
    rows = execute_query_pg(
        """
        SELECT group_label
        FROM account_groups
        WHERE client_id = %s AND LOWER(group_label) = LOWER(%s)
        ORDER BY is_active DESC, id
        LIMIT 1
        """,
        (client_id, requested_label),
    )
    return rows[0]["group_label"] if rows else requested_label


def _parse_report_period(period_start: Any, period_end: Any) -> tuple[pd.Timestamp, pd.Timestamp]:
    try:
        start = pd.Timestamp(period_start).normalize()
        end = pd.Timestamp(period_end).normalize()
    except (TypeError, ValueError) as error:
        raise HTTPException(400, "Reporting period dates must be valid ISO dates") from error
    if pd.isna(start) or pd.isna(end) or start > end:
        raise HTTPException(400, "Reporting period must have a valid inclusive start and end")
    return start, end


def _report_diagnostic(code: str, severity: str, message: str, **extra: Any) -> dict[str, Any]:
    item = {"code": code, "severity": severity, "message": message}
    item.update(extra)
    return item


def _json_value(value: Any) -> Any:
    """Convert pandas/numpy/date values into safe JSON response values."""
    if value is None:
        return None
    if isinstance(value, (pd.Timestamp, datetime, date)):
        return value.isoformat()
    if hasattr(value, "item") and not isinstance(value, (str, bytes)):
        try:
            value = value.item()
        except (ValueError, TypeError):
            pass
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    return value


def _frame_records(frame: Any, limit: int = 200) -> list[dict[str, Any]]:
    if frame is None or not isinstance(frame, pd.DataFrame) or frame.empty:
        return []
    records = frame.head(limit).to_dict("records")
    return [{str(key): _json_value(value) for key, value in row.items()} for row in records]


def _frame_records_all(frame: Any) -> list[dict[str, Any]]:
    if frame is None or not isinstance(frame, pd.DataFrame) or frame.empty:
        return []
    records = frame.to_dict('records')
    return [{str(key): _json_value(value) for key, value in row.items()} for row in records]


def _category_total_records(frame: Any) -> list[dict[str, Any]]:
    if frame is None or not isinstance(frame, pd.DataFrame) or frame.empty:
        return []
    indexed = frame.reset_index()
    first_column = indexed.columns[0]
    if first_column != "category":
        indexed = indexed.rename(columns={first_column: "category"})
    return _frame_records(indexed)


def _report_preview(summary: dict[str, Any] | None, cross_group_check: pd.DataFrame | None) -> dict[str, Any]:
    if summary is None:
        return {
            "months": [],
            "category_totals": [],
            "monthly_balance_checks": [],
            "categories_needing_review": [],
            "reconciliation_proof": [],
            "cross_group_check": _frame_records(cross_group_check),
            "selected_voucher_count": 0,
            "internal_transfer_count": 0,
            "category_review_count": 0,
        }
    review = summary.get("categories_needing_review")
    return {
        "months": [str(month) for month in summary.get("months", [])],
        "category_totals": _category_total_records(summary.get("category_totals")),
        "monthly_balance_checks": _frame_records(summary.get("monthly_balance_checks")),
        "categories_needing_review": _frame_records(review),
        "reconciliation_proof": _frame_records(summary.get("reconciliation_proof")),
        "cross_group_check": _frame_records(cross_group_check),
        "selected_voucher_count": len(summary.get("selected_vouchers", [])),
        "internal_transfer_count": len(summary.get("internal_transfers", [])),
        "category_review_count": int(len(review)) if isinstance(review, pd.DataFrame) else 0,
    }


def _build_report_result(
    client_id: int,
    group_label: str,
    period_start: Any,
    period_end: Any,
    *,
    journal_upload_id: int | None = None,
    journal_template_id: int | None = None,
) -> dict[str, Any]:
    """Build a report preview and retain private engine objects for download."""
    _require_active_client(client_id)
    requested_group = normalize_group_label(group_label)
    if not requested_group:
        raise HTTPException(400, "Reporting group is required")
    start, end = _parse_report_period(period_start, period_end)
    stored_group = _stored_group_label(client_id, requested_group)
    group_codes = sorted(_saved_codes(client_id, stored_group))
    if not group_codes:
        raise HTTPException(409, "Save the account group before generating a report")

    diagnostics: list[dict[str, Any]] = []
    try:
        source = load_ledger_report_source(
            client_id,
            journal_upload_id=journal_upload_id,
            journal_template_id=journal_template_id,
            period_start=start,
            period_end=end,
            query_fn=execute_query_pg,
        )
    except ValueError as error:
        raise HTTPException(400, str(error)) from error
    diagnostics.extend(source.diagnostics)

    balance_source = load_reconciliation_balance_source(
        client_id,
        stored_group,
        group_codes,
        start,
        end,
        query_fn=execute_query_pg,
    )
    diagnostics.extend(balance_source.diagnostics)

    other_group = None
    if stored_group.casefold() == "bank":
        other_group = "Cash"
    elif stored_group.casefold() == "cash":
        other_group = "Bank"
    related_codes = sorted(_saved_codes(client_id, other_group)) if other_group else []
    summary: dict[str, Any] | None = None
    cross_group_check: pd.DataFrame | None = None
    controls: dict[str, dict[str, Any]] = {
        "ledger_balance": {"status": "Blocked", "message": "The report has not been calculated."},
        "categories": {"status": "Blocked", "count": 0, "message": "Category coverage is unavailable."},
        "transfers": {"status": "Blocked", "count": 0, "message": "Transfer schedule is unavailable."},
        "cross_group": {"status": "Not configured", "message": "No paired Bank/Cash group is configured."},
        "reconciliation": {"status": "Blocked", "message": "Reconciliation proof is unavailable."},
    }

    if source.ready and balance_source.ready:
        try:
            summary = build_group_summary(
                source.journal,
                source.coa,
                group_codes,
                start,
                end,
                group_label=stored_group,
                related_group_codes=related_codes,
                related_group_label=other_group,
                opening_balances=balance_source.opening_balances,
                actual_closing_balances=balance_source.actual_closing_balances,
            )
        except LedgerBalanceError as error:
            diagnostics.append(
                _report_diagnostic(
                    "unbalanced_vouchers",
                    "error",
                    str(error),
                    checks=_frame_records(error.checks, limit=20),
                )
            )
        except ValueError as error:
            diagnostics.append(_report_diagnostic("report_input_error", "error", str(error)))

    if summary is not None:
        review_count = len(summary.get("categories_needing_review", []))
        transfer_count = len(summary.get("internal_transfers", []))
        controls["ledger_balance"] = {"status": "Balanced", "message": "All selected vouchers balance."}
        controls["categories"] = {
            "status": "Review" if review_count else "Clear",
            "count": review_count,
            "message": (
                f"{review_count} contra account line(s) need category review."
                if review_count
                else "All contra account lines have report categories."
            ),
        }
        controls["transfers"] = {
            "status": "Ready",
            "count": transfer_count,
            "message": (
                f"{transfer_count} internal transfer line(s) are shown separately."
                if transfer_count
                else "No internal transfers were detected."
            ),
        }
        proof = summary.get("reconciliation_proof")
        proof_matched = isinstance(proof, pd.DataFrame) and not proof.empty and bool(proof["status"].eq("Matched").all())
        controls["reconciliation"] = {
            "status": "Matched" if proof_matched else "Variance",
            "message": "Opening balances plus movements agree to actual closing balances." if proof_matched else summary.get("reconciliation_message") or "Reconciliation has a variance.",
        }

        if related_codes and other_group:
            try:
                other_summary = build_group_summary(
                    source.journal,
                    source.coa,
                    related_codes,
                    start,
                    end,
                    group_label=other_group,
                    related_group_codes=group_codes,
                    related_group_label=stored_group,
                )
                current_category = f"Received/Paid to {other_group}"
                other_category = f"Received/Paid to {stored_group}"
                cross_group_check = build_cross_group_check(
                    summary, other_summary, current_category, other_category, right_multiplier=-1
                )
                matched = not cross_group_check.empty and bool(cross_group_check["status"].eq("Matched").all())
                controls["cross_group"] = {
                    "status": "Matched" if matched else "Variance",
                    "message": "Paired Bank/Cash movements agree." if matched else "Paired Bank/Cash movements have a variance.",
                }
            except LedgerBalanceError as error:
                diagnostics.append(_report_diagnostic("cross_group_unbalanced", "error", str(error)))
                controls["cross_group"] = {"status": "Blocked", "message": "The paired group contains an unbalanced voucher."}

    blocking_reasons = [item["message"] for item in diagnostics if item.get("severity") == "error"]
    if controls["categories"]["status"] == "Review":
        blocking_reasons.append(controls["categories"]["message"])
    if controls["cross_group"]["status"] == "Variance":
        blocking_reasons.append(controls["cross_group"]["message"])
    if controls["reconciliation"]["status"] != "Matched":
        blocking_reasons.append(controls["reconciliation"]["message"])
    can_download = summary is not None and not blocking_reasons

    public = {
        "client_id": client_id,
        "group_label": stored_group,
        "period_start": start.date().isoformat(),
        "period_end": end.date().isoformat(),
        "source": {
            "metadata": dict(source.metadata),
            "diagnostics": diagnostics,
            "ready": source.ready and balance_source.ready,
            "balance_months": balance_source.months,
        },
        "controls": controls,
        "blocking_reasons": blocking_reasons,
        "can_download": can_download,
        "preview": _report_preview(summary, cross_group_check),
    }
    public["_summary"] = summary
    public["_cross_group_check"] = cross_group_check
    public["_opening_balances"] = balance_source.opening_balances
    return public


@router.get("/api/reconciliation/groups")
def list_account_groups(
    client_id: int = Query(..., ge=1),
    session=Depends(require_admin),
):
    """Return active exact-code membership for every configured group."""
    _require_active_client(client_id)
    rows = execute_query_pg(
        """
        SELECT group_label, account_code, updated_at
        FROM account_groups
        WHERE client_id = %s AND is_active = TRUE
        ORDER BY LOWER(group_label), account_code
        """,
        (client_id,),
    )
    groups: dict[str, dict[str, Any]] = {}
    for row in rows:
        group = groups.setdefault(
            row["group_label"],
            {"group_label": row["group_label"], "account_codes": [], "updated_at": row.get("updated_at")},
        )
        group["account_codes"].append(row["account_code"])
    return {"client_id": client_id, "groups": list(groups.values())}


@router.get("/api/reconciliation/account-suggestions")
def list_account_group_suggestions(
    client_id: int = Query(..., ge=1),
    group_label: Optional[str] = Query(None),
    session=Depends(require_admin),
):
    """Suggest Bank/Cash membership from Trial Balance account type only."""
    _require_active_client(client_id)
    accounts = _latest_trial_balance_accounts(client_id)
    if not accounts:
        raise HTTPException(404, "No parsed trial balance found for this client")
    selected = _saved_codes(client_id, group_label)
    return {
        "client_id": client_id,
        "group_label": normalize_group_label(group_label) if group_label else None,
        "accounts": [
            {
                "id": row["id"],
                "account_code": row["account_code"],
                "account_name": row["account_name"],
                "account_type": row["source_account_type"],
                "classification": row["classification"],
                "is_leaf": row["classification"] == "leaf",
                "suggested_group": _suggested_group(row["source_account_type"]),
                "is_selected": row["account_code"] in selected,
            }
            for row in accounts
        ],
    }


@router.put("/api/reconciliation/groups/{client_id}")
def save_account_group(
    client_id: int,
    payload: AccountGroupUpdate,
    session=Depends(require_admin),
):
    """Replace one group's active membership using exact Trial Balance codes."""
    _require_active_client(client_id)
    group_label, account_codes = _clean_group_request(payload)
    accounts = _latest_trial_balance_accounts(client_id)
    if not accounts:
        raise HTTPException(409, "A parsed trial balance is required before saving account groups")

    valid_codes = {normalize_account_code(row["account_code"]) for row in accounts}
    unknown_codes = [code for code in account_codes if code not in valid_codes]
    if unknown_codes:
        raise HTTPException(
            400,
            detail={
                "message": "One or more account codes are not present in the latest parsed Trial Balance",
                "account_codes": unknown_codes,
            },
        )

    group_label = _stored_group_label(client_id, group_label)
    transaction = pg_transaction() if pg_transaction is not None else nullcontext()
    with transaction:
        execute_write_pg(
            """
            UPDATE account_groups
            SET is_active = FALSE, updated_at = CURRENT_TIMESTAMP
            WHERE client_id = %s AND LOWER(group_label) = LOWER(%s) AND is_active = TRUE
            """,
            (client_id, group_label),
        )
        for account_code in account_codes:
            execute_write_pg(
                """
                INSERT INTO account_groups (client_id, group_label, account_code, is_active)
                VALUES (%s, %s, %s, TRUE)
                ON CONFLICT (client_id, group_label, account_code)
                DO UPDATE SET is_active = TRUE, updated_at = CURRENT_TIMESTAMP
                """,
                (client_id, group_label, account_code),
            )

    log_audit("Admin", "Update", "Reconciliation", f"Saved {group_label} account group for client {client_id}")
    return {
        "client_id": client_id,
        "group_label": group_label,
        "account_codes": account_codes,
        "count": len(account_codes),
    }


@router.get("/api/reconciliation/balances")
def list_reconciliation_balances(
    client_id: int = Query(..., ge=1),
    group_label: Optional[str] = Query(None),
    session=Depends(require_admin),
):
    """Return saved opening and actual closing balances for a client/group."""

    _require_active_client(client_id)
    filters = ["client_id = %s"]
    params: list[Any] = [client_id]
    if group_label:
        filters.append("LOWER(group_label) = LOWER(%s)")
        params.append(normalize_group_label(group_label))
    rows = execute_query_pg(
        """
        SELECT group_label, account_code, period_month, opening_balance,
               actual_closing_balance, updated_at
        FROM report_balance_inputs
        WHERE """ + " AND ".join(filters) + " ORDER BY LOWER(group_label), account_code, period_month",
        tuple(params),
    )
    return {"client_id": client_id, "balances": rows}


@router.put("/api/reconciliation/balances/{client_id}")
def replace_reconciliation_balances(
    client_id: int,
    payload: ReconciliationBalanceInputsUpdate,
    session=Depends(require_admin),
):
    """Replace one group's standardized balance-input table transactionally."""

    _require_active_client(client_id)
    group_label, balances = _clean_balance_request(payload)
    accounts = _latest_trial_balance_accounts(client_id)
    if not accounts:
        raise HTTPException(409, "A parsed trial balance is required before saving balances")
    valid_codes = {normalize_account_code(row["account_code"]) for row in accounts}
    unknown_codes = [item["account_code"] for item in balances if item["account_code"] not in valid_codes]
    if unknown_codes:
        raise HTTPException(
            400,
            detail={
                "message": "One or more balance account codes are not present in the latest parsed Trial Balance",
                "account_codes": unknown_codes,
            },
        )
    group_codes = _saved_codes(client_id, group_label)
    if not group_codes:
        raise HTTPException(409, "Save the account group before saving reconciliation balances")
    outside_group = [item["account_code"] for item in balances if item["account_code"] not in group_codes]
    if outside_group:
        raise HTTPException(
            400,
            detail={
                "message": "One or more balance account codes are not members of the selected account group",
                "account_codes": outside_group,
            },
        )

    group_label = _stored_group_label(client_id, group_label)
    transaction = pg_transaction() if pg_transaction is not None else nullcontext()
    with transaction:
        execute_write_pg(
            "DELETE FROM report_balance_inputs WHERE client_id = %s AND LOWER(group_label) = LOWER(%s)",
            (client_id, group_label),
        )
        for item in balances:
            execute_write_pg(
                """
                INSERT INTO report_balance_inputs
                    (client_id, group_label, account_code, period_month, opening_balance, actual_closing_balance)
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (
                    client_id,
                    group_label,
                    item["account_code"],
                    item["period_month"],
                    item["opening_balance"],
                    item["actual_closing_balance"],
                ),
            )

    log_audit("Admin", "Update", "Reconciliation", f"Saved {group_label} balance inputs for client {client_id}")
    return {
        "client_id": client_id,
        "group_label": group_label,
        "balances": balances,
        "count": len(balances),
    }


@router.get("/api/reconciliation/category-sets")
def list_report_category_sets(
    client_id: int = Query(..., ge=1),
    session=Depends(require_admin),
):
    """List active report-specific category sets for a client."""
    _require_active_client(client_id)
    rows = execute_query_pg(
        """
        SELECT s.id, s.purpose_key, s.set_name, s.is_active, s.updated_at,
               COUNT(DISTINCT c.id) FILTER (WHERE c.is_active = TRUE) AS category_count,
               COUNT(DISTINCT a.id) AS assignment_count
        FROM report_category_sets s
        LEFT JOIN report_categories c ON c.category_set_id = s.id
        LEFT JOIN report_account_category_assignments a ON a.category_set_id = s.id
        WHERE s.client_id = %s AND s.is_active = TRUE
        GROUP BY s.id
        ORDER BY s.purpose_key
        """,
        (client_id,),
    )
    return {"client_id": client_id, "category_sets": rows}


@router.post("/api/reconciliation/category-sets")
def create_report_category_set(
    client_id: int = Query(..., ge=1),
    payload: ReportCategorySetCreate = ...,
    session=Depends(require_admin),
):
    """Create one client-scoped category set without touching Audit Verification categories."""
    _require_active_client(client_id)
    purpose_key, set_name = _clean_report_category_set_request(payload)
    existing = execute_query_pg(
        """
        SELECT id FROM report_category_sets
        WHERE client_id = %s AND purpose_key = %s
        """,
        (client_id, purpose_key),
    )
    if existing:
        raise HTTPException(409, "A category set already exists for this client and purpose")
    category_set_id = execute_write_pg(
        """
        INSERT INTO report_category_sets (client_id, purpose_key, set_name, is_active)
        VALUES (%s, %s, %s, TRUE)
        RETURNING id
        """,
        (client_id, purpose_key, set_name),
    )
    if category_set_id is None:
        raise HTTPException(500, "The report category set could not be created")
    log_audit("Admin", "Create", "Reconciliation", f"Created {purpose_key} report category set for client {client_id}")
    return {
        "id": category_set_id,
        "client_id": client_id,
        "purpose_key": purpose_key,
        "set_name": set_name,
        "is_active": True,
        "category_count": 0,
        "assignment_count": 0,
    }


@router.get("/api/reconciliation/category-sets/{category_set_id}/categories")
def list_report_categories(
    category_set_id: int,
    session=Depends(require_admin),
):
    category_set = _get_report_category_set(category_set_id)
    rows = execute_query_pg(
        """
        SELECT id, category_key, label, parent_category_key, sort_order, is_active, updated_at
        FROM report_categories
        WHERE category_set_id = %s
        ORDER BY sort_order, LOWER(label), category_key
        """,
        (category_set_id,),
    )
    return {"category_set": category_set, "categories": rows}


@router.put("/api/reconciliation/category-sets/{category_set_id}/categories")
def replace_report_categories(
    category_set_id: int,
    payload: ReportCategoryDefinitionsUpdate,
    session=Depends(require_admin),
):
    """Replace active definitions while preserving the category set and old assignment history."""
    category_set = _get_report_category_set(category_set_id)
    categories = _clean_report_categories(payload)
    transaction = pg_transaction() if pg_transaction is not None else nullcontext()
    with transaction:
        execute_write_pg(
            """
            UPDATE report_categories
            SET is_active = FALSE, updated_at = CURRENT_TIMESTAMP
            WHERE category_set_id = %s AND is_active = TRUE
            """,
            (category_set_id,),
        )
        for category in categories:
            execute_write_pg(
                """
                INSERT INTO report_categories
                    (category_set_id, category_key, label, parent_category_key, sort_order, is_active)
                VALUES (%s, %s, %s, %s, %s, TRUE)
                ON CONFLICT (category_set_id, category_key)
                DO UPDATE SET
                    label = EXCLUDED.label,
                    parent_category_key = EXCLUDED.parent_category_key,
                    sort_order = EXCLUDED.sort_order,
                    is_active = TRUE,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (
                    category_set_id,
                    category["category_key"],
                    category["label"],
                    category["parent_category_key"],
                    category["sort_order"],
                ),
            )
    log_audit("Admin", "Update", "Reconciliation", f"Saved report categories for set {category_set_id}")
    return {
        "category_set_id": category_set_id,
        "purpose_key": category_set["purpose_key"],
        "categories": categories,
        "count": len(categories),
    }


@router.get("/api/reconciliation/category-sets/{category_set_id}/assignments")
def list_report_category_assignments(
    category_set_id: int,
    session=Depends(require_admin),
):
    category_set = _get_report_category_set(category_set_id)
    rows = execute_query_pg(
        """
        SELECT a.account_code, c.category_key, c.label AS category_label, a.updated_at
        FROM report_account_category_assignments a
        JOIN report_categories c ON c.id = a.category_id
        WHERE a.category_set_id = %s AND c.is_active = TRUE
        ORDER BY a.account_code
        """,
        (category_set_id,),
    )
    return {"category_set": category_set, "assignments": rows}


@router.put("/api/reconciliation/category-sets/{category_set_id}/assignments")
def replace_report_category_assignments(
    category_set_id: int,
    payload: ReportCategoryAssignmentsUpdate,
    session=Depends(require_admin),
):
    """Replace account-code mappings for one report category set."""
    category_set = _get_report_category_set(category_set_id)
    assignments = _clean_report_assignments(payload)
    accounts = _latest_trial_balance_accounts(category_set["client_id"])
    if not accounts:
        raise HTTPException(409, "A parsed trial balance is required before saving report categories")

    valid_codes = {normalize_account_code(row["account_code"]) for row in accounts}
    unknown_codes = [item["account_code"] for item in assignments if item["account_code"] not in valid_codes]
    if unknown_codes:
        raise HTTPException(
            400,
            detail={
                "message": "One or more account codes are not present in the latest parsed Trial Balance",
                "account_codes": unknown_codes,
            },
        )

    category_rows = execute_query_pg(
        """
        SELECT id, category_key
        FROM report_categories
        WHERE category_set_id = %s AND is_active = TRUE
        """,
        (category_set_id,),
    )
    category_ids = {normalize_report_key(row["category_key"]): row["id"] for row in category_rows}
    missing_categories = sorted({item["category_key"] for item in assignments if item["category_key"] not in category_ids})
    if missing_categories:
        raise HTTPException(
            400,
            detail={
                "message": "One or more category keys are not active in this category set",
                "category_keys": missing_categories,
            },
        )

    transaction = pg_transaction() if pg_transaction is not None else nullcontext()
    with transaction:
        execute_write_pg(
            "DELETE FROM report_account_category_assignments WHERE category_set_id = %s",
            (category_set_id,),
        )
        for item in assignments:
            execute_write_pg(
                """
                INSERT INTO report_account_category_assignments
                    (client_id, category_set_id, account_code, category_id)
                VALUES (%s, %s, %s, %s)
                """,
                (
                    category_set["client_id"],
                    category_set_id,
                    item["account_code"],
                    category_ids[item["category_key"]],
                ),
            )
    log_audit("Admin", "Update", "Reconciliation", f"Saved report category assignments for set {category_set_id}")
    return {
        "category_set_id": category_set_id,
        "purpose_key": category_set["purpose_key"],
        "assignments": assignments,
        "count": len(assignments),
    }


def _cashflow_configuration(client_id: int) -> dict[str, Any]:
    rows = execute_query_pg(
        """
        WITH request AS (
            SELECT %s::bigint AS treasury_client_id,
                   %s::bigint AS settings_client_id,
                   %s::bigint AS role_client_id
        )
        SELECT
            COALESCE((
                SELECT json_agg(json_build_object(
                    'account_code', a.account_code,
                    'category_key', c.category_key,
                    'label', c.label
                ) ORDER BY a.account_code)
                FROM account_control_cashflow_category_assignments a
                JOIN account_control_cashflow_categories c
                  ON c.id = a.category_id
                 AND c.client_id = a.client_id
                 AND c.is_active = TRUE
                WHERE a.client_id = (SELECT settings_client_id FROM request)
            ), '[]'::json) AS mappings,
            COALESCE((
                SELECT json_agg(json_build_object(
                    'account_code', t.account_code,
                    'treasury_type', t.treasury_type
                ) ORDER BY t.treasury_type, t.account_code)
                FROM account_control_cashflow_treasury_accounts t
                WHERE t.client_id = (SELECT treasury_client_id FROM request) AND t.is_active = TRUE
            ), '[]'::json) AS treasury,
            COALESCE((
                SELECT json_agg(a.account_code ORDER BY a.account_code)
                FROM account_control_assignments a
                WHERE a.client_id = (SELECT settings_client_id FROM request)
                  AND a.verification_workstream IN ('local_vendor', 'import_vendor')
            ), '[]'::json) AS vendor_accounts,
            COALESCE((
                SELECT json_build_object(
                    'match_internal_transfers', s.match_internal_transfers,
                    'receipt_presentation', s.receipt_presentation,
                    'source_upload_id', s.source_upload_id,
                    'source_template_id', s.source_template_id
                )
                FROM account_control_cashflow_settings s
                WHERE s.client_id = (SELECT settings_client_id FROM request)
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
                WHERE r.client_id = (SELECT role_client_id FROM request)
            ), '[]'::json) AS customer_payment_roles,
            COALESCE((
                SELECT revision
                FROM cashflow_report_revisions
                WHERE client_id = (SELECT settings_client_id FROM request)
            ), 1) AS cashflow_report_revision
        """,
        (client_id, client_id, client_id),
    )
    row = rows[0] if rows else {}
    mappings = row.get("mappings") or []
    treasury = row.get("treasury") or []
    settings = row.get("settings") or {}
    customer_payment_roles = {
        "customer_receipt": [],
        "bank_charge": [],
        "withholding_tax": [],
        "other_deduction": [],
    }
    for role_row in row.get("customer_payment_roles") or []:
        role = str(role_row.get("role_key") or "")
        if role in customer_payment_roles and role_row.get("label"):
            customer_payment_roles[role].append(role_row["label"])
    return {
        "category_mappings": {normalize_account_code(row["account_code"]): row.get("label") or "" for row in mappings},
        "vendor_account_codes": [normalize_account_code(code) for code in row.get("vendor_accounts") or []],
        "cash_account_codes": [normalize_account_code(row["account_code"]) for row in treasury if row["treasury_type"] == "cash"],
        "source_upload_id": settings.get("source_upload_id"),
        "source_template_id": settings.get("source_template_id"),
        "bank_account_codes": [normalize_account_code(row["account_code"]) for row in treasury if row["treasury_type"] == "bank"],
        "match_internal_transfers": bool(settings.get("match_internal_transfers")),
        "receipt_presentation": settings.get("receipt_presentation") or "net",
        "cashflow_report_revision": max(int(row.get("cashflow_report_revision") or 1), 1),
        "customer_payment_roles": customer_payment_roles,
    }

def _cashflow_template_mapping_view(client_id: int, template_id: int) -> dict[str, Any]:
    template_rows = execute_query_pg(
        "SELECT id, template_name, is_system FROM templates WHERE id = %s",
        (template_id,),
    )
    if not template_rows:
        raise HTTPException(404, "Transaction template not found")
    columns = execute_query_pg(
        """
        SELECT mc.id AS template_column_id, mc.column_name, mc.data_type, tcm.sort_order
        FROM template_column_mappings tcm
        JOIN master_columns mc ON mc.id = tcm.column_id
        WHERE tcm.template_id = %s AND mc.is_active = TRUE
        ORDER BY tcm.sort_order, mc.id
        """,
        (template_id,),
    )
    saved_rows = execute_query_pg(
        """
        SELECT m.role, m.template_column_id, c.column_name
        FROM account_control_cashflow_source_mappings m
        JOIN master_columns c ON c.id = m.template_column_id
        WHERE m.client_id = %s AND m.template_id = %s
        ORDER BY m.role
        """,
        (client_id, template_id),
    )
    saved = {row["role"]: row for row in saved_rows}
    column_by_id = {row["template_column_id"]: row for row in columns}
    roles = []
    for role, label in CASHFLOW_COLUMN_ROLE_LABELS.items():
        mapped = saved.get(role)
        column = column_by_id.get(mapped["template_column_id"]) if mapped else None
        roles.append(
            {
                "role": role,
                "label": label,
                "required": role in CASHFLOW_REQUIRED_COLUMN_ROLES,
                "template_column_id": column["template_column_id"] if column else None,
                "column_name": column["column_name"] if column else None,
            }
        )
    missing_roles = [
        role for role in CASHFLOW_REQUIRED_COLUMN_ROLES
        if role not in saved or saved[role]["template_column_id"] not in column_by_id
    ]
    return {
        "client_id": client_id,
        "template": dict(template_rows[0]),
        "columns": [dict(row) for row in columns],
        "roles": roles,
        "missing_required_roles": missing_roles,
        "ready": not missing_roles,
    }


@router.get("/api/reconciliation/cashflows/template-columns")
def get_cashflow_template_columns(
    client_id: int = Query(..., ge=1),
    template_id: int = Query(..., ge=1),
    session=Depends(require_admin),
):
    _require_active_client(client_id)
    return _cashflow_template_mapping_view(client_id, template_id)


@router.put("/api/reconciliation/cashflows/template-columns")
def save_cashflow_template_columns(
    payload: CashflowTemplateMappingUpdate,
    session=Depends(require_admin),
):
    _require_active_client(payload.client_id)
    template_rows = execute_query_pg(
        "SELECT id FROM templates WHERE id = %s",
        (payload.template_id,),
    )
    if not template_rows:
        raise HTTPException(404, "Transaction template not found")
    column_rows = execute_query_pg(
        """
        SELECT mc.id AS template_column_id
        FROM template_column_mappings tcm
        JOIN master_columns mc ON mc.id = tcm.column_id
        WHERE tcm.template_id = %s AND mc.is_active = TRUE
        """,
        (payload.template_id,),
    )
    allowed_columns = {row["template_column_id"] for row in column_rows}
    cleaned: dict[str, int] = {}
    for item in payload.mappings:
        role = str(item.role or "").strip().casefold()
        if role not in CASHFLOW_COLUMN_ROLE_LABELS:
            raise HTTPException(400, f"Unsupported Cashflows mapping role: {item.role}")
        if role in cleaned:
            raise HTTPException(400, f"Cashflows mapping role is repeated: {role}")
        if item.template_column_id not in allowed_columns:
            raise HTTPException(400, f"Template column {item.template_column_id} is not part of the selected template")
        if item.template_column_id in cleaned.values():
            raise HTTPException(400, "Each template column can be mapped only once")
        cleaned[role] = item.template_column_id
    missing_roles = [role for role in CASHFLOW_REQUIRED_COLUMN_ROLES if role not in cleaned]
    if missing_roles:
        raise HTTPException(400, {"message": "Required Cashflows roles are missing", "fields": missing_roles})
    actor = (session or {}).get("username") or "Admin"
    transaction = pg_transaction() if pg_transaction is not None else nullcontext()
    with transaction:
        execute_write_pg(
            "DELETE FROM account_control_cashflow_source_mappings WHERE client_id = %s AND template_id = %s",
            (payload.client_id, payload.template_id),
        )
        for role, column_id in cleaned.items():
            execute_write_pg(
                """INSERT INTO account_control_cashflow_source_mappings
                   (client_id, template_id, role, template_column_id, updated_by, updated_at)
                   VALUES (%s, %s, %s, %s, %s, CURRENT_TIMESTAMP)""",
                (payload.client_id, payload.template_id, role, column_id, actor),
            )
        if payload.set_as_default:
            execute_write_pg(
                """INSERT INTO account_control_cashflow_settings
                   (client_id, source_template_id, updated_by, updated_at)
                   VALUES (%s, %s, %s, CURRENT_TIMESTAMP)
                   ON CONFLICT (client_id) DO UPDATE SET
                     source_template_id = EXCLUDED.source_template_id,
                     updated_by = EXCLUDED.updated_by,
                     updated_at = CURRENT_TIMESTAMP""",
                (payload.client_id, payload.template_id, actor),
            )
    invalidate_cashflow_summary_read_models(
        payload.client_id,
        reason="template_mapping_updated",
        changed_by=actor,
        write_fn=execute_write_pg,
    )
    log_audit("Admin", "Update", "Cashflows", f"Saved template mapping for client {payload.client_id}, template {payload.template_id}")
    return _cashflow_template_mapping_view(payload.client_id, payload.template_id)



def _cashflow_source_options(client_id: int) -> list[dict[str, Any]]:
    rows = execute_query_pg(
        """
        SELECT cd.upload_id, cd.template_id, t.template_name, f.file_name,
               (SELECT s.source_upload_id FROM account_control_cashflow_settings s WHERE s.client_id = cd.client_id) AS selected_upload_id,
               (SELECT s.source_template_id FROM account_control_cashflow_settings s WHERE s.client_id = cd.client_id) AS selected_template_id,
               COUNT(*) AS source_row_count,
               MIN(cd.extract_date) AS period_start,
               MAX(cd.extract_date) AS period_end
        FROM client_data cd
        LEFT JOIN templates t ON t.id = cd.template_id
        LEFT JOIN uploaded_files f ON f.id = cd.upload_id
        WHERE cd.client_id = %s AND cd.is_deleted = FALSE
        GROUP BY cd.client_id, cd.upload_id, cd.template_id, t.template_name, f.file_name
        ORDER BY MAX(cd.extract_date) DESC NULLS LAST,
                 cd.upload_id DESC NULLS LAST,
                 cd.template_id DESC NULLS LAST
        """,
        (client_id,),
    )
    options: list[dict[str, Any]] = []
    for row in rows:
        upload_id = row.get("upload_id")
        template_id = row.get("template_id")
        selector_type = "upload" if upload_id is not None else "template"
        selector_id = upload_id if upload_id is not None else template_id
        options.append(
            {
                "selector_type": selector_type,
                "selector_id": selector_id,
                "upload_id": upload_id,
                "template_id": template_id,
                "template_name": row.get("template_name") or "Unknown template",
                "file_name": row.get("file_name") or "",
                "source_row_count": int(row.get("source_row_count") or 0),
                "period_start": row.get("period_start"),
                "period_end": row.get("period_end"),
            }
        )
        if row.get("selected_upload_id") is not None:
            options[-1]["selected_upload_id"] = row.get("selected_upload_id")
        if row.get("selected_template_id") is not None:
            options[-1]["selected_template_id"] = row.get("selected_template_id")
    return options


def _cashflow_source_selection_response(client_id: int, sources: list[dict[str, Any]]) -> dict[str, Any]:
    response: dict[str, Any] = {"client_id": client_id, "sources": sources}
    selected_upload_ids = {item.get("selected_upload_id") for item in sources if item.get("selected_upload_id") is not None}
    selected_template_ids = {item.get("selected_template_id") for item in sources if item.get("selected_template_id") is not None}
    if selected_upload_ids:
        response["selected_upload_id"] = next(iter(selected_upload_ids))
    if selected_template_ids:
        response["selected_template_id"] = next(iter(selected_template_ids))
    return response


def _cashflow_preview(
    summary: dict[str, Any] | None,
    detail_sections: set[str] | None = None,
    treasury_view: str = "combined",
    customer_account_code: str = "",
) -> dict[str, Any]:
    """Format the summary and only the child detail sections requested by the caller."""
    treasury_view = normalize_cashflow_treasury_view(treasury_view)
    if summary is None:
        return {
            'ledger_rollups': [],
            'drilldown_controls': {},
            'drilldown_details_prepared': False,
            "months": [],
            "periods": [],
            "group_by": "month",
            "category_totals": [],
            "treasury_category_totals": [],
            "treasury_reconciliation": {},
            "treasury_movement_proof": [],
            "treasury_view": treasury_view,
            "treasury_view_ready": treasury_view == "combined",
            "treasury_view_totals": [],
            "treasury_view_reconciliation": {},
            "transfer_details": [],
            "transfer_reconciliation": None,
            "customer_payment_details": [],
            "customer_payment_customer_totals": [],
            "customer_payment_period_totals": [],
            "customer_payment_customer_options": [],
            "customer_payment_reconciliation": {},
            "customer_payment_selected_account_code": "",
            "deduction_details": [],
            "categories_needing_review": [],
            "selected_voucher_count": 0,
            "internal_transfer_count": 0,
            "category_review_count": 0,
            "deduction_count": 0,
            "period_metrics": {},
            "receipt_presentation": "net",
            "detail_sections_loaded": [],
        }
    loaded = {"transfers", "customer_payments", "vendor_payments", "deductions", "review"} if detail_sections is None else set(detail_sections)
    review = summary.get("categories_needing_review")
    customer_rows = _frame_records_all(summary.get("customer_payment_customer_totals")) if "customer_payments" in loaded else []
    customer_period_rows = _frame_records_all(summary.get("customer_payment_period_totals")) if "customer_payments" in loaded else []
    customer_options = []
    seen_customer_codes: set[str] = set()
    for row in customer_rows:
        code = str(row.get("customer_account_code") or "").strip()
        if code and code not in seen_customer_codes:
            seen_customer_codes.add(code)
            customer_options.append({"account_code": code, "account_name": str(row.get("customer_account_name") or "")})
    selected_customer_code = str(customer_account_code or "").strip()
    if selected_customer_code:
        selected_key = selected_customer_code.casefold()
        customer_rows = [
            row for row in customer_rows
            if str(row.get("customer_account_code") or "").strip().casefold() == selected_key
        ]
    treasury_projection = project_cashflow_treasury_view(summary, treasury_view)
    treasury_view_totals = (
        _category_total_records(treasury_projection["totals"])
        if treasury_projection["totals_shape"] == "category"
        else _frame_records_all(treasury_projection["totals"])
    )
    return {
        'ledger_rollups': _frame_records_all(summary.get('cashflow_ledger_rollups')) if 'drilldown' in loaded else [],
        'drilldown_controls': summary.get('cashflow_drilldown_controls') if 'drilldown' in loaded else {},
        'drilldown_details_prepared': bool(summary.get('cashflow_drilldown_prepared')) if 'drilldown' in loaded else False,
        "months": [str(month) for month in summary.get("months", [])],
        "periods": summary.get("periods", []),
        "group_by": summary.get("group_by", "month"),
        "category_totals": _category_total_records(summary.get("category_totals")),
        "treasury_category_totals": _frame_records_all(summary.get("treasury_category_totals")),
        "treasury_reconciliation": summary.get("treasury_reconciliation") or {},
        "treasury_movement_proof": list(summary.get("treasury_movement_proof") or []),
        "treasury_view": treasury_projection["view"],
        "treasury_view_ready": bool(treasury_projection["ready"]),
        "treasury_view_totals": treasury_view_totals,
        "treasury_view_reconciliation": treasury_projection["reconciliation"],
        "transfer_details": _frame_records_all(summary.get("transfer_details")) if "transfers" in loaded else [],
        "transfer_reconciliation": summary.get("transfer_reconciliation") if "transfers" in loaded else None,
        "customer_payment_details": _frame_records_all(summary.get("customer_payment_details")) if "customer_payments" in loaded else [],
        "customer_payment_customer_totals": customer_rows,
        "customer_payment_period_totals": customer_period_rows,
        "customer_payment_account_period_totals": _frame_records_all(summary.get("customer_payment_account_period_totals")) if "customer_payments" in loaded else [],
        **{key: _frame_records_all(summary.get(key)) if "vendor_payments" in loaded else []
           for key in ("vendor_payment_details", "vendor_payment_vendor_totals", "vendor_payment_period_totals")},
        "customer_payment_customer_options": customer_options,
        "customer_payment_reconciliation": dict(summary.get("customer_payment_reconciliation") or {}) if "customer_payments" in loaded else {},
        "customer_payment_selected_account_code": selected_customer_code,
        # The same payload supplies XLSX; a preview cap would omit audit detail.
        "deduction_details": _frame_records_all(summary.get("deduction_details")) if "deductions" in loaded else [],
        "categories_needing_review": _frame_records(review) if "review" in loaded else [],
        "selected_voucher_count": int(summary.get("selected_voucher_count", 0)),
        "internal_transfer_count": int(summary.get("internal_transfer_count", 0)),
        "category_review_count": int(summary.get("category_review_count", len(review))) if review is not None else int(summary.get("category_review_count", 0)),
        "deduction_count": int(len(summary.get("deduction_details"))) if summary.get("deduction_details") is not None else 0,
        "period_metrics": summary.get("period_metrics") or {},
        "receipt_presentation": summary.get("receipt_presentation") or "net",
        "detail_sections_loaded": sorted(loaded),
    }


def _cashflow_cache_can_serve_details(
    cached_summary: dict[str, Any] | None,
    detail_sections: set[str] | None,
    treasury_view: str = "combined",
) -> bool:
    if cached_summary is None:
        return False
    metadata = cached_summary.get("metadata", {})
    payload_version = metadata.get("summary_payload_version")
    if payload_version is not None and int(payload_version or 0) < CASHFLOW_SUMMARY_PAYLOAD_VERSION:
        return False
    if treasury_view != "combined" and int(metadata.get("treasury_totals_version", 0) or 0) < CASHFLOW_TREASURY_TOTALS_VERSION:
        return False
    if detail_sections:
        metadata = cached_summary.get("metadata", {})
        if 'drilldown' in detail_sections:
            if not (
                bool(metadata.get('drilldown_details_prepared'))
                and int(metadata.get('drilldown_details_version', 0) or 0) >= CASHFLOW_DRILLDOWN_DETAILS_VERSION
            ):
                return False
            detail_sections = detail_sections - {'drilldown'}
        if not detail_sections:
            return True
        return (
            bool(metadata.get("child_details_prepared"))
            and int(metadata.get("child_details_version", 0) or 0) >= CASHFLOW_CHILD_DETAILS_VERSION
        )
    return True


def _build_cashflow_result(
    client_id: int,
    period_start: Any,
    period_end: Any,
    detail_sections: set[str] | None = None,
    group_by: str = "month",
    treasury_view: str = "combined",
    force_refresh: bool = False,
    customer_account_code: str = "",
    warm_ledger_cache: bool = False,
    include_balances: bool = False,
) -> dict[str, Any]:
    request_started = perf_counter()
    validation_started = perf_counter()
    _require_active_client(client_id)
    validation_seconds = perf_counter() - validation_started
    start, end = _parse_report_period(period_start, period_end)
    try:
        grouping = normalize_cashflow_grouping(group_by)
        treasury_view = normalize_cashflow_treasury_view(treasury_view)
    except ValueError as error:
        raise HTTPException(400, str(error)) from error
    configuration_started = perf_counter()
    configuration = _cashflow_configuration(client_id)
    configuration_seconds = perf_counter() - configuration_started
    # Phase 1: Cashflows are sourced directly from active client_data rows and
    # their derived_* columns. Preparation/read-model tables are no longer part
    # of the active report path.
    preparation_status = {
        "client_id": client_id,
        "status": "not_required",
        "ready": True,
        "updating": False,
        "failed": False,
        "fallback_active": False,
        "active_preparation_revision": 0,
        "requested_preparation_revision": 0,
        "affected_row_count": 0,
        "latest_job": None,
    }
    configured_template_id = configuration.get("source_template_id")
    report_revision = max(int(configuration.get("cashflow_report_revision") or 1), 1)
    diagnostics: list[dict[str, Any]] = []
    include_drilldown = bool(detail_sections and "drilldown" in detail_sections)
    source_started = perf_counter()
    summary_read_model_lookup_seconds = 0.0
    prepared_snapshot_lookup_seconds = 0.0
    prepared_rollup_lookup_seconds = 0.0
    source_build_seconds = 0.0
    cache_mode = "client_data_source"
    cached_summary = None
    source_build_started = perf_counter()
    try:
        source = load_ledger_report_source(
            client_id,
            journal_template_id=configured_template_id,
            period_start=start,
            period_end=end,
            cashflow_group_by=grouping,
            query_fn=execute_query_pg,
            account_category_source="cashflow",
            treasury_account_codes=[
                *configuration["cash_account_codes"],
                *configuration["bank_account_codes"],
            ],
        )
    except ValueError as error:
        raise HTTPException(400, str(error)) from error
    finally:
        source_build_seconds = perf_counter() - source_build_started
    source_seconds = perf_counter() - source_started
    source_metadata_timings = source.metadata
    canonical_selection_seconds = float(source_metadata_timings.get("cashflow_source_identity_seconds") or 0.0)
    classification_query_seconds = float(source_metadata_timings.get("cashflow_classification_query_seconds") or 0.0)
    source_transform_seconds = float(source_metadata_timings.get("cashflow_source_transform_seconds") or 0.0)
    prepared_source_mode = str(source_metadata_timings.get("cashflow_report_source_mode") or "fallback")
    prepared_revision = int(source_metadata_timings.get("preparation_revision") or 0)
    diagnostics.extend(source.diagnostics)
    if configured_template_id is None:
        diagnostics.append(_report_diagnostic("cashflow_template_not_configured", "error", "Configure and map the Transaction Report template in Account Control Centre before generating Cashflows."))
    if not configuration["cash_account_codes"] and not configuration["bank_account_codes"]:
        diagnostics.append(_report_diagnostic("no_treasury_accounts", "error", "Select at least one Cash or Bank account in Account Control Centre before generating Cashflows."))

    summary: dict[str, Any] | None = cached_summary["summary"] if cached_summary is not None else None
    summary_seconds = 0.0
    summary_status = (cached_summary["metadata"].get("summary_read_model") if cached_summary else None) or "skipped_source_not_ready"
    if summary is None and source.ready and not any(item.get("severity") == "error" for item in diagnostics):
        summary_started = perf_counter()
        summary_status = "completed"
        try:
            summary = build_cashflow_summary(
                source.journal,
                source.coa,
                configuration["cash_account_codes"],
                configuration["bank_account_codes"],
                start,
                end,
                match_internal_transfers=configuration["match_internal_transfers"],
                receipt_presentation=configuration["receipt_presentation"],
                cashflow_role_categories=configuration.get("customer_payment_roles"),
                vendor_account_codes=configuration.get("vendor_account_codes", []),
                group_by=grouping,
                include_drilldown=warm_ledger_cache or bool(detail_sections and 'drilldown' in detail_sections),
            )
        except LedgerBalanceError as error:
            summary_status = "failed"
            diagnostics.append(_report_diagnostic("unbalanced_cashflow_vouchers", "error", str(error), checks=_frame_records(error.checks, limit=20)))
        except ValueError as error:
            summary_status = "failed"
            diagnostics.append(_report_diagnostic("cashflow_input_error", "error", str(error)))
        finally:
            summary_seconds = perf_counter() - summary_started
    review_count = int(summary.get("category_review_count", 0)) if summary else 0
    transfer_count = int(summary.get("internal_transfer_count", 0)) if summary else 0
    controls = {
        "source": {"status": "Ready" if source.ready else "Blocked", "message": "Client Data source is ready." if source.ready else "Resolve the source diagnostics before generating Cashflows."},
        "treasury": {"status": "Ready" if configuration["cash_account_codes"] or configuration["bank_account_codes"] else "Blocked", "message": f"{len(configuration['cash_account_codes'])} Cash and {len(configuration['bank_account_codes'])} Bank account(s) configured."},
        "categories": {"status": "Review" if review_count else ("Ready" if summary else "Blocked"), "count": review_count, "message": f"{review_count} counter-account line(s) need category review." if review_count else "Counter-account categories are ready."},
        "transfers": {"status": "Ready" if summary else "Blocked", "count": transfer_count, "message": f"{transfer_count} internal transfer voucher(s) identified." if summary else "Transfer analysis is unavailable."},
    }
    blocking_reasons = [item["message"] for item in diagnostics if item.get("severity") == "error"]
    if review_count:
        blocking_reasons.append(controls["categories"]["message"])
    source_metadata = dict(source.metadata)
    source_metadata["cashflow_report_revision"] = report_revision
    source_metadata["cashflow_preparation_status"] = preparation_status["status"]
    freshness_generated_at = source_metadata.get("summary_read_model_created_at") or datetime.now().astimezone().isoformat()
    source_quality = {
        "source_row_count": int(source_metadata.get("source_row_count") or 0),
        "valid_row_count": int(source_metadata.get("valid_row_count") or len(source.journal)),
        "rejected_row_count": int(source_metadata.get("rejected_row_count") or 0),
        "layout_row_count": int(source_metadata.get("layout_row_count") or 0),
        "out_of_period_row_count": int(source_metadata.get("out_of_period_row_count") or 0),
        "rejected_rows_sample": source_metadata.get("rejected_rows_sample") or [],
    }
    preview_started = perf_counter()
    preview = _cashflow_preview(summary, detail_sections, treasury_view, customer_account_code)
    if include_balances:
        # The public opt-in runs the whole report in one read-only snapshot.
        # Existing views/drilldowns retain their previous query and response path.
        accounts = {code: 'Cash' for code in configuration['cash_account_codes'] if treasury_view != 'bank'}
        accounts.update({code: 'Bank' for code in configuration['bank_account_codes'] if treasury_view != 'cash'})
        periods = summary.get('periods', []) if summary else []
        def view_movements(view):
            # Category allocation and balance arithmetic have separate proofs.
            # Missing/old proofs must never silently fall back to weighted totals.
            proof = {row['period_key']: row for row in (summary or {}).get('treasury_movement_proof', [])
                     if row.get('treasury_type') == view}
            return {p['key']: (Decimal(str(proof[p['key']]['explained_movement']))
                              if p['key'] in proof and proof[p['key']].get('status') == 'Matched' else None)
                    for p in periods}
        movements = view_movements('combined' if treasury_view == 'split' else treasury_view)
        split_movements = {key: view_movements(key) for key in ('bank', 'cash')} if treasury_view == 'split' else None
        account_names = {item['account_code']: item['account_name'] for item in (_latest_trial_balance_accounts(client_id) if accounts and periods else [])
                         if item['account_code'] in accounts}
        preview['balance_reconciliation'] = load_uploaded_ledger_balances(
            client_id, accounts, periods, movements, query_fn=execute_query_pg,
            account_names=account_names, treasury_movements=split_movements)
        # Mixed is a category-attribution bucket, never a third Treasury balance.
        preview['balance_reconciliation']['unresolved_movements'] = {
            p['key']: (0.0 if movements[p['key']] is not None and (split_movements is None or
                all(split_movements[view][p['key']] is not None for view in ('bank', 'cash'))) else None)
            for p in periods}
    configuration_key = _cashflow_configuration_key(configuration)
    ledger_context = {
        'client_id': client_id, 'template_id': configured_template_id,
        'period_start': start.date().isoformat(), 'period_end': end.date().isoformat(),
        'group_by': grouping, 'treasury_view': treasury_view,
        'report_revision': report_revision,
        'source_key': source_metadata.get('cashflow_report_source_key') or '',
        'configuration_key': configuration_key,
    }
    if warm_ledger_cache:
        frame = summary.get('cashflow_ledger_rollups') if summary else None
        count = len(frame) if isinstance(frame, pd.DataFrame) else 0
        if summary is None or count > CASHFLOW_BROWSER_LEDGER_MAX_ROWS:
            preview['ledger_cache'] = {'context': ledger_context, 'complete': False, 'row_count': count, 'rows': []}
        else:
            preview['ledger_cache'] = _cashflow_browser_ledger_payload(_frame_records_all(frame), ledger_context)
    format_seconds = perf_counter() - preview_started
    total_seconds = perf_counter() - request_started
    cache_hit = cached_summary is not None
    read_model = str((cached_summary.get("metadata") or {}).get("summary_read_model") or "none") if cached_summary else "none"
    authoritative = bool(cache_mode == "client_data_source" or force_refresh)
    logger.info(
        "cashflows_performance client_id=%s period_start=%s period_end=%s group_by=%s details=%s "
        "validation_seconds=%.3f configuration_seconds=%.3f source_seconds=%.3f "
        "summary_read_model_lookup_seconds=%.3f prepared_rollup_lookup_seconds=%.3f "
        "prepared_snapshot_lookup_seconds=%.3f "
        "source_build_seconds=%.3f canonical_selection_seconds=%.3f "
        "classification_query_seconds=%.3f source_transform_seconds=%.3f "
        "summary_seconds=%.3f format_seconds=%.3f total_seconds=%.3f "
        "source_rows=%s valid_rows=%s selected_vouchers=%s summary_status=%s cache_mode=%s "
        "prepared_source_mode=%s prepared_revision=%s "
        "cache_hit=%s read_model=%s report_revision=%s authoritative=%s",
        client_id,
        start.date().isoformat(),
        end.date().isoformat(),
        grouping,
        ",".join(sorted(detail_sections)) if detail_sections is not None else "all",
        validation_seconds,
        configuration_seconds,
        source_seconds,
        summary_read_model_lookup_seconds,
        prepared_rollup_lookup_seconds,
        prepared_snapshot_lookup_seconds,
        source_build_seconds,
        canonical_selection_seconds,
        classification_query_seconds,
        source_transform_seconds,
        summary_seconds,
        format_seconds,
        total_seconds,
        source_quality["source_row_count"],
        source_quality["valid_row_count"],
        int(summary.get("selected_voucher_count", 0)) if summary else 0,
        summary_status,
        cache_mode,
        prepared_source_mode,
        prepared_revision,
        cache_hit,
        read_model,
        report_revision,
        authoritative,
    )
    return {
        "client_id": client_id,
        "period_start": start.date().isoformat(),
        "period_end": end.date().isoformat(),
        "group_by": grouping,
        "treasury_view": treasury_view,
        "cashflow_report_revision": report_revision,
        "cashflow_configuration_key": configuration_key,
        "ledger_context": ledger_context,
        "preparation": preparation_status,
        "freshness": {
            "mode": cache_mode,
            "report_revision": report_revision,
            "generated_at": freshness_generated_at,
            "authoritative": authoritative,
        },
        "configuration": {
            "cash_account_count": len(configuration["cash_account_codes"]),
            "bank_account_count": len(configuration["bank_account_codes"]),
            "source_template_id": configured_template_id,
            "match_internal_transfers": configuration["match_internal_transfers"],
            "receipt_presentation": configuration["receipt_presentation"],
        },
        "source": {"metadata": source_metadata, "quality": source_quality, "diagnostics": diagnostics, "ready": source.ready and not any(item.get("severity") == "error" for item in diagnostics)},
        "database_totals": source_metadata.get("database_totals"),
        "controls": controls,
        "blocking_reasons": blocking_reasons,
        "preview": preview,
    }


def _build_financial_statement_result(
    client_id: int,
    period_start: Any,
    period_end: Any,
    *,
    journal_upload_id: int | None = None,
    journal_template_id: int | None = None,
) -> dict[str, Any]:
    """Build a read-only preview from central Account Control mappings."""
    _require_active_client(client_id)
    start, end = _parse_report_period(period_start, period_end)
    diagnostics: list[dict[str, Any]] = []
    try:
        source = load_ledger_report_source(
            client_id,
            journal_upload_id=journal_upload_id,
            journal_template_id=journal_template_id,
            period_start=start,
            period_end=end,
            query_fn=execute_query_pg,
            account_category_source="financial_statement",
        )
    except ValueError as error:
        raise HTTPException(400, str(error)) from error
    diagnostics.extend(source.diagnostics)
    summary: dict[str, Any] | None = None
    if source.ready:
        try:
            summary = build_financial_statement_summary(source.journal, source.coa, start, end)
        except ValueError as error:
            diagnostics.append(_report_diagnostic("financial_statement_input_error", "error", str(error)))

    unmapped_count = int(summary.get("unmapped_account_count", 0)) if summary else 0
    if not source.ready or any(item.get("severity") == "error" for item in diagnostics):
        mapping_status = "Blocked"
        mapping_message = "The financial statement preview cannot be calculated until the source diagnostics are resolved."
    elif unmapped_count:
        mapping_status = "Review"
        mapping_message = f"{unmapped_count} account code(s) have journal activity but no active Financial Statement category."
    else:
        mapping_status = "Ready"
        mapping_message = "All journal account codes with activity have an active Financial Statement category."

    account_counts = []
    if summary is not None:
        account_counts = [
            {"category": str(category), "account_count": int(count)}
            for category, count in summary["account_counts"].items()
        ]
    preview = {
        "months": summary["months"] if summary else [],
        "category_totals": _category_total_records(summary["category_totals"]) if summary else [],
        "account_counts": account_counts,
        "unmapped_accounts": _frame_records(summary["unmapped_accounts"]) if summary else [],
        "selected_voucher_count": summary["selected_voucher_count"] if summary else 0,
        "source_line_count": summary["source_line_count"] if summary else 0,
        "mapped_account_count": summary["mapped_account_count"] if summary else 0,
        "unmapped_account_count": unmapped_count,
    }
    return {
        "client_id": client_id,
        "period_start": start.date().isoformat(),
        "period_end": end.date().isoformat(),
        "source": {
            "metadata": dict(source.metadata),
            "diagnostics": diagnostics,
            "ready": source.ready and not any(item.get("severity") == "error" for item in diagnostics),
        },
        "controls": {
            "financial_statement_mapping": {"status": mapping_status, "message": mapping_message, "unmapped_account_count": unmapped_count},
        },
        "blocking_reasons": [item["message"] for item in diagnostics if item.get("severity") == "error"],
        "preview": preview,
    }


def _public_report_result(result: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in result.items() if not key.startswith("_")}


def _parse_cashflow_detail_sections(value: str | None) -> set[str] | None:
    """Parse optional lazy child-section selection; omitted means legacy full response."""
    if value is None:
        return None
    raw = value.strip().casefold()
    if raw in {"", "summary"}:
        return set()
    allowed = {"transfers", "customer_payments", "vendor_payments", "deductions", "review"}
    allowed = allowed | {'drilldown'}
    sections = {item.strip() for item in raw.split(",") if item.strip()}
    if sections - allowed:
        raise HTTPException(400, "Invalid Cashflows detail section requested.")
    return sections

@router.get("/api/reconciliation/cashflows/revision")
def cashflow_revision(
    client_id: int = Query(..., ge=1),
    session=Depends(require_admin),
):
    """Return the durable revision and compact source identity for browser caches."""
    _require_active_client(client_id)
    request_started = perf_counter()
    configuration_started = perf_counter()
    configuration = _cashflow_configuration(client_id)
    configuration_seconds = perf_counter() - configuration_started
    template_id = configuration.get("source_template_id")
    source_key = None
    source_identity_seconds = 0.0
    if template_id is not None:
        source_key = f"client-data-v1:{client_id}:{int(template_id)}"
    report_revision = max(int(configuration.get("cashflow_report_revision") or 1), 1)
    logger.info(
        "cashflows_revision_performance",
        extra={
            "client_id": client_id,
            "configuration_seconds": configuration_seconds,
            "source_identity_seconds": source_identity_seconds,
            "total_seconds": perf_counter() - request_started,
            "report_revision": report_revision,
            "source_key_present": bool(source_key),
        },
    )
    return {
        "client_id": client_id,
        "cashflow_report_revision": report_revision,
        "cashflow_report_source_key": source_key,
        "cashflow_configuration_key": _cashflow_configuration_key(configuration),
    }


@router.get("/api/reconciliation/cashflows/preparation-status")
def cashflow_preparation_status(
    client_id: int = Query(..., ge=1),
    session=Depends(require_admin),
):
    """Return the durable Cashflows preparation state for setup/report status displays."""
    _require_active_client(client_id)
    return get_cashflow_preparation_status(execute_query_pg, client_id=client_id)


def _run_cashflow_preparation_retry_background(job_id: int, client_id: int) -> None:
    result = run_cashflow_preparation_job_postgres(job_id)
    logger.info("cashflows_preparation_retry client_id=%s job_id=%s result=%s", client_id, job_id, result)
    try:
        prepare_cashflow_summary_snapshot(client_id)
    except Exception as error:
        logger.warning("cashflows_snapshot_preparation_failed client_id=%s job_id=%s error=%s", client_id, job_id, error)


@router.post("/api/reconciliation/cashflows/preparation-retry")
def retry_cashflow_preparation(
    client_id: int = Query(..., ge=1),
    job_id: int = Query(..., ge=1),
    background_tasks: BackgroundTasks = None,
    session=Depends(require_admin),
):
    """Retry one failed preparation job while retaining its previous active revision."""
    _require_active_client(client_id)
    with pg_transaction() as conn:
        try:
            job = retry_cashflow_preparation_job(conn, job_id=job_id, client_id=client_id)
        except LookupError as error:
            raise HTTPException(404, "Cashflows preparation job not found") from error
        except ValueError as error:
            raise HTTPException(409, str(error)) from error
    if background_tasks is None:
        _run_cashflow_preparation_retry_background(job_id, client_id)
    else:
        background_tasks.add_task(_run_cashflow_preparation_retry_background, job_id, client_id)
    return job


@router.get('/api/reconciliation/cashflows/differences')
def cashflow_differences(
    client_id: int = Query(..., ge=1), period_start: str = Query(...), period_end: str = Query(...),
    treasury_view: str = Query('combined'), differences_only: bool = Query(True),
    page: int = Query(1, ge=1), limit: int = Query(50, ge=1, le=200), session=Depends(require_admin),
):
    """On-demand diagnostic only: never alters the summary/export path."""
    start, end = _parse_report_period(period_start, period_end)
    try:
        view = normalize_cashflow_treasury_view(treasury_view)
    except ValueError as error:
        raise HTTPException(400, str(error)) from error
    def read(_conn):
        _require_active_client(client_id)
        config = _cashflow_configuration(client_id)
        if not config.get('source_template_id'):
            raise HTTPException(400, 'Configure the Cashflows Transaction Report template first.')
        accounts = {c: 'Bank' for c in config['bank_account_codes'] if view != 'cash'}
        accounts.update({c: 'Cash' for c in config['cash_account_codes'] if view != 'bank'})
        source = load_cashflow_difference_source(client_id, config['source_template_id'], list(accounts),
            start.date().isoformat(), end.date().isoformat(), query_fn=execute_query_pg)
        result = build_cashflow_differences(source, accounts, start.date().isoformat(), end.date().isoformat())
        rows = result.pop('findings')
        filtered = [row for row in rows if not differences_only or row['status'] != 'matched']
        page_count = max(1, math.ceil(len(filtered) / limit))
        current = min(page, page_count)
        result.update(client_id=client_id, treasury_view=view, configuration_key=_cashflow_configuration_key(config),
            report_revision=config.get('cashflow_report_revision'), total_groups=len(rows), total_rows=len(filtered),
            page=current, page_count=page_count, limit=limit, rows=filtered[(current-1)*limit:current*limit])
        return result
    return run_read_snapshot_pg(read)


def _reconciliation_cache_context(client_id, start, end, grouping, view, config):
    """Called only within the same read-only snapshot as the figures/validation."""
    codes = sorted(set(config['bank_account_codes'] + config['cash_account_codes']))
    stamp = load_cashflow_reconciliation_identity(client_id, config['source_template_id'], codes, query_fn=execute_query_pg)
    configuration_key = _cashflow_configuration_key(config)
    context = dict(cache_schema=1, client_id=client_id, template_id=config['source_template_id'],
        period_start=start.date().isoformat(), period_end=end.date().isoformat(), group_by=grouping,
        treasury_view=view, configuration_key=configuration_key, report_revision=config.get('cashflow_report_revision'))
    context['cache_identity'] = hashlib.sha256(json.dumps([context, stamp], sort_keys=True, default=str).encode()).hexdigest()
    return context


@router.get('/api/reconciliation/cashflows/reconciliation/cache-validation')
def validate_cashflow_reconciliation_cache(
    client_id: int = Query(..., ge=1), period_start: str = Query(...), period_end: str = Query(...),
    group_by: str = Query('month'), treasury_view: str = Query('combined'), session=Depends(require_admin),
):
    """Validate both source populations without rebuilding the accounting bridge."""
    start, end = _parse_report_period(period_start, period_end)
    try:
        grouping = normalize_cashflow_grouping(group_by)
        view = normalize_cashflow_treasury_view(treasury_view)
    except ValueError as error:
        raise HTTPException(400, str(error)) from error
    def read(_conn):
        _require_active_client(client_id)
        config = _cashflow_configuration(client_id)
        if not config.get('source_template_id') or not (config['bank_account_codes'] or config['cash_account_codes']):
            raise HTTPException(400, 'Configure the Cashflows template and Cash/Bank accounts first.')
        return _reconciliation_cache_context(client_id, start, end, grouping, view, config)
    return run_read_snapshot_pg(read)


@router.get('/api/reconciliation/cashflows/reconciliation')
def cashflow_reconciliation(
    client_id: int = Query(..., ge=1), period_start: str = Query(...), period_end: str = Query(...),
    group_by: str = Query('month'), treasury_view: str = Query('combined'),
    support_key: str = Query('', max_length=64), period_key: str = Query('', max_length=40),
    expected_fingerprint: str = Query('', max_length=64),
    posting_differences: bool = Query(False), comparison_view: str = Query('', max_length=16),
    differences_only: bool = Query(True),
    page: int = Query(1, ge=1), limit: int = Query(50, ge=1, le=200), session=Depends(require_admin),
):
    """Build the on-demand working paper; normal preview/export never calls it."""
    start, end = _parse_report_period(period_start, period_end)
    try:
        grouping = normalize_cashflow_grouping(group_by)
        view = normalize_cashflow_treasury_view(treasury_view)
    except ValueError as error:
        raise HTTPException(400, str(error)) from error

    def read(_conn):
        _require_active_client(client_id)
        config = _cashflow_configuration(client_id)
        if not config.get('source_template_id'):
            raise HTTPException(400, 'Configure the Cashflows Transaction Report template first.')
        accounts = {code: 'Bank' for code in config['bank_account_codes']}
        accounts.update({code: 'Cash' for code in config['cash_account_codes']})
        if not accounts:
            raise HTTPException(400, 'Configure Cash/Bank accounts before reconciling Cashflows.')
        report_source = load_ledger_report_source(client_id, journal_template_id=config['source_template_id'],
            period_start=start, period_end=end, cashflow_group_by=grouping, query_fn=execute_query_pg,
            account_category_source='cashflow', treasury_account_codes=list(accounts))
        if not report_source.ready:
            raise HTTPException(400, 'Cashflow source is unavailable; resolve its source diagnostics first.')
        try:
            summary = build_cashflow_summary(report_source.journal, report_source.coa,
                config['cash_account_codes'], config['bank_account_codes'], start, end,
                group_by=grouping, match_internal_transfers=config['match_internal_transfers'],
                receipt_presentation=config['receipt_presentation'], cashflow_role_categories=config['customer_payment_roles'],
                vendor_account_codes=config.get('vendor_account_codes', []), include_reconciliation_evidence=True)
            source = load_cashflow_difference_source(client_id, config['source_template_id'], list(accounts),
                start.date().isoformat(), end.date().isoformat(), query_fn=execute_query_pg)
            result = build_cashflow_reconciliation(summary, source, accounts, view)
        except ValueError as error:
            raise HTTPException(400, str(error)) from error
        support = result.pop('_support_entries')
        config_key = _cashflow_configuration_key(config)
        result['evidence_fingerprint'] = hashlib.sha256((result['evidence_fingerprint'] + config_key).encode()).hexdigest()
        if expected_fingerprint and expected_fingerprint != result['evidence_fingerprint']:
            raise HTTPException(409, 'Reconciliation evidence changed; reload the working paper before opening support.')
        if period_key and period_key not in {p['key'] for p in summary['periods']}:
            raise HTTPException(400, 'Unknown reconciliation period.')
        if posting_differences:
            if support_key or period_key:
                raise HTTPException(400, 'Posting differences cover the whole selected range; request adjustment support separately.')
            selected_view = comparison_view or ('combined' if view == 'split' else view)
            if selected_view not in result['views']:
                raise HTTPException(400, 'Unknown reconciliation comparison view.')
            codes = {code: kind for code, kind in accounts.items()
                     if selected_view == 'combined' or kind.casefold() == selected_view}
            # Reuse this working paper's snapshot and fingerprint, including in Bank/Cash views.
            comparison = build_cashflow_differences(source, codes, start.date().isoformat(), end.date().isoformat())
            rows = comparison.pop('findings')
            filtered = [row for row in rows if not differences_only or row['status'] != 'matched']
            page_count = max(1, (len(filtered) + limit - 1) // limit)
            current = min(page, page_count)
            comparison.update(treasury_view=selected_view, total_groups=len(rows), total_rows=len(filtered),
                page=current, page_count=page_count, limit=limit, rows=filtered[(current-1)*limit:current*limit])
            result['posting_differences'] = comparison
        if support_key:
            if support_key not in support:
                raise HTTPException(400, 'Unknown reconciliation adjustment.')
            entries = [row for row in support[support_key] if not period_key or row['period_key'] == period_key]
            support_view = support[support_key][0]['treasury_view']
            codes = [code for code, kind in accounts.items() if support_view == 'combined' or kind.casefold() == support_view]
            result['support'] = cashflow_reconciliation_support_page(source, entries, codes, periods=summary['periods'], page=page, limit=limit)
        result.update(_reconciliation_cache_context(client_id, start, end, grouping, view, config))
        return result

    return run_read_snapshot_pg(read)


@router.get("/api/reconciliation/cashflows/preview")
def preview_cashflows(
    client_id: int = Query(..., ge=1),
    period_start: str = Query(...),
    period_end: str = Query(...),
    group_by: str = Query("month"),
    treasury_view: str = Query("combined"),
    customer_account_code: str = Query(""),
    details: Optional[str] = Query(None, description="summary or comma-separated child sections"),
    refresh_from_source: bool = Query(False, alias="refresh"),
    warm_ledger_cache: bool = Query(False, description="Include bounded account roll-ups from this calculation"),
    include_balances: bool = Query(False, description="Include current uploaded-ledger balance evidence"),
    session=Depends(require_admin),
):
    """Return Cashflows using the Transaction Report template configured in Account Control Centre."""
    if not isinstance(group_by, str):
        group_by = getattr(group_by, "default", "month")
    if not isinstance(treasury_view, str):
        treasury_view = getattr(treasury_view, "default", "combined")
    detail_value = details if isinstance(details, str) else None
    if not isinstance(refresh_from_source, bool):
        refresh_from_source = bool(getattr(refresh_from_source, "default", False))
    options = dict(detail_sections=_parse_cashflow_detail_sections(detail_value), group_by=group_by,
                   treasury_view=treasury_view, force_refresh=refresh_from_source,
                   customer_account_code=customer_account_code,
                   warm_ledger_cache=warm_ledger_cache if isinstance(warm_ledger_cache, bool) else False)
    if include_balances is True:
        return run_read_snapshot_pg(lambda _conn: _build_cashflow_result(
            client_id, period_start, period_end, **options, include_balances=True))
    return _build_cashflow_result(client_id, period_start, period_end, **options)

def _filter_cashflow_ledger_rows(
    rows: list[dict[str, Any]],
    *,
    search: str = "",
    category: str = "",
    account_head: str = "",
    treasury_type: str = "",
    period_key: str = "",
) -> list[dict[str, Any]]:
    search_text = search.strip().casefold() if isinstance(search, str) else ""
    category_text = category.strip().casefold() if isinstance(category, str) else ""
    account_head_text = account_head.strip().casefold() if isinstance(account_head, str) else ""
    treasury_text = treasury_type.strip().casefold() if isinstance(treasury_type, str) else ""
    period_text = period_key.strip().casefold() if isinstance(period_key, str) else ""

    def matches(row: dict[str, Any]) -> bool:
        fields = (
            row.get("category"),
            row.get("account_code"),
            row.get("account_name"),
            row.get("period_label"),
        )
        searchable = " ".join(str(value or "") for value in fields).casefold()
        account_head_label = _cashflow_ledger_account_head_label(row).casefold()
        account_head_values = {
            str(row.get("account_name") or "").casefold(),
            str(row.get("account_code") or "").casefold(),
            account_head_label,
        }
        return (
            (not search_text or search_text in searchable)
            and (not category_text or str(row.get("category") or "").casefold() == category_text)
            and (not account_head_text or account_head_text in account_head_values)
            and (not treasury_text or str(row.get("treasury_type") or "").casefold() == treasury_text)
            and (not period_text or str(row.get("period_key") or "").casefold() == period_text)
        )

    return [row for row in rows if matches(row)]



def _filter_cashflow_treasury_totals(
    records: list[dict[str, Any]],
    *,
    summary_filter: str = "",
    hide_zero_rows: bool = False,
) -> list[dict[str, Any]]:
    filter_text = summary_filter.strip().casefold() if isinstance(summary_filter, str) else ""
    categories: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        category = str(record.get("category") or "")
        if filter_text and filter_text not in category.casefold():
            continue
        categories.setdefault(category, []).append(record)
    if not hide_zero_rows:
        return [record for rows in categories.values() for record in rows]
    visible: list[dict[str, Any]] = []
    for rows in categories.values():
        has_amount = any(
            abs(float(record.get(field) or 0)) > 0
            for record in rows
            for field in ("bank_amount", "cash_amount", "mixed_amount", "total_amount")
        )
        if has_amount:
            visible.extend(rows)
    return visible

def _filter_cashflow_category_totals(
    records: list[dict[str, Any]],
    period_keys: list[str],
    *,
    summary_filter: str = "",
    hide_zero_rows: bool = False,
) -> list[dict[str, Any]]:
    filter_text = summary_filter.strip().casefold() if isinstance(summary_filter, str) else ""
    filtered: list[dict[str, Any]] = []
    for record in records:
        category = str(record.get("category") or "")
        if filter_text and filter_text not in category.casefold():
            continue
        values = [float(record.get(key) or 0) for key in period_keys]
        total = record.get("Total")
        total_value = float(total) if total is not None else sum(values)
        if hide_zero_rows and not any(abs(value) > 0 for value in values) and abs(total_value) <= 0:
            continue
        filtered.append(record)
    return filtered


def _cashflow_ledger_account_head_label(row: dict[str, Any]) -> str:
    account_name = str(row.get("account_name") or "").strip()
    account_code = str(row.get("account_code") or "").strip()
    if account_name and account_code:
        return f"{account_name} / {account_code}"
    return account_name or account_code


def _cashflow_ledger_account_head_options(
    rows: list[dict[str, Any]],
    *,
    treasury_type: str = "",
    period_key: str = "",
) -> list[dict[str, str]]:
    scoped_rows = _filter_cashflow_ledger_rows(rows, treasury_type=treasury_type, period_key=period_key)
    options: dict[tuple[str, str], dict[str, str]] = {}
    for row in scoped_rows:
        category = str(row.get("category") or "").strip()
        value = _cashflow_ledger_account_head_label(row)
        if not category or not value:
            continue
        options[(category.casefold(), value.casefold())] = {"category": category, "value": value}
    return sorted(options.values(), key=lambda option: (option["category"].casefold(), option["value"].casefold()))


@router.get('/api/reconciliation/cashflows/drilldown')
def cashflow_drilldown(
    client_id: int = Query(..., ge=1),
    period_start: str = Query(...),
    period_end: str = Query(...),
    group_by: str = Query('month'),
    search: str = Query('', max_length=200),
    category: str = Query('', max_length=200),
    account_head: str = Query('', max_length=300),
    treasury_type: str = Query('', max_length=40),
    period_key: str = Query('', max_length=80),
    treasury_view: str = Query('combined'),
    page: int = Query(1, ge=1),
    limit: int = Query(50, ge=1, le=200),
    session=Depends(require_admin),
):
    '''Return server-filtered, paginated compact Cashflows ledger roll-ups.'''
    request_started = perf_counter()
    build_started = perf_counter()
    projection_mode = "full_report"
    result = _build_cashflow_result(
        client_id,
        period_start,
        period_end,
        detail_sections={'drilldown'},
        group_by=group_by,
        treasury_view=treasury_view,
    )
    build_seconds = perf_counter() - build_started
    filter_started = perf_counter()
    preview = result.get('preview') or {}
    treasury_view = result.get('treasury_view') or normalize_cashflow_treasury_view(treasury_view)
    requested_treasury_type = treasury_type
    if not treasury_type and treasury_view in {'bank', 'cash'}:
        treasury_type = treasury_view
    rows = preview.get('ledger_rollups') or []
    search_text = search.strip().casefold() if isinstance(search, str) else ''
    category_text = category.strip().casefold() if isinstance(category, str) else ''
    account_head_text = account_head.strip().casefold() if isinstance(account_head, str) else ''
    treasury_text = treasury_type.strip().casefold() if isinstance(treasury_type, str) else ''
    period_text = period_key.strip().casefold() if isinstance(period_key, str) else ''
    requested_treasury_text = requested_treasury_type.strip().casefold() if isinstance(requested_treasury_type, str) else ''
    base_request = not any((search_text, category_text, account_head_text, requested_treasury_text, period_text))

    account_head_options = _cashflow_ledger_account_head_options(rows, treasury_type=treasury_type, period_key=period_key)
    filtered_rows = _filter_cashflow_ledger_rows(
        rows,
        search=search,
        category=category,
        account_head=account_head,
        treasury_type=treasury_type,
        period_key=period_key,
    )
    total_rows = len(filtered_rows)
    page_count = max(1, math.ceil(total_rows / limit))
    current_page = min(page, page_count)
    start_index = (current_page - 1) * limit
    page_rows = filtered_rows[start_index:start_index + limit]
    filter_seconds = perf_counter() - filter_started
    freshness = result.get("freshness") or {}
    cache_mode = str(freshness.get("mode") or "unknown")
    cache_hit = cache_mode in {"hit", "prepared_snapshot", "cache_hit"}
    logger.info(
        "cashflows_drilldown_performance client_id=%s period_start=%s period_end=%s group_by=%s "
        "filter_count=%s build_seconds=%.3f filter_seconds=%.3f total_seconds=%.3f "
        "source_rows=%s total_rows=%s returned_rows=%s requested_page=%s page=%s page_count=%s limit=%s "
        "cache_mode=%s cache_hit=%s projection_mode=%s report_revision=%s",
        client_id,
        result['period_start'],
        result['period_end'],
        result['group_by'],
        sum(bool(value) for value in (search_text, category_text, account_head_text, treasury_text, period_text)),
        build_seconds,
        filter_seconds,
        perf_counter() - request_started,
        int((result.get('source') or {}).get('quality', {}).get('source_row_count') or 0),
        total_rows,
        len(page_rows),
        page,
        current_page,
        page_count,
        limit,
        cache_mode,
        cache_hit,
        projection_mode,
        int(freshness.get("report_revision") or result.get("cashflow_report_revision") or 1),
    ),
    baseline = _cashflow_browser_ledger_payload(rows, result.get('ledger_context') or {}) if base_request else None
    return {
        'client_id': client_id,
        'period_start': result['period_start'],
        'period_end': result['period_end'],
        'group_by': result['group_by'],
        'treasury_view': treasury_view,
        'rows': page_rows,
        'base_rows': baseline['rows'] if baseline and baseline['complete'] else None,
        'ledger_context': result.get('ledger_context'),
        'cashflow_report_revision': result.get('cashflow_report_revision'),
        'cashflow_configuration_key': result.get('cashflow_configuration_key'),
        'account_head_options': account_head_options,
        'total_rows': total_rows,
        'page': current_page,
        'limit': limit,
        'page_count': page_count,
        'controls': preview.get('drilldown_controls') or {},
        'source': result.get('source') or {},
        'database_totals': result.get('database_totals'),
        'blocking_reasons': result.get('blocking_reasons') or [],
    }

@router.get("/api/reconciliation/cashflows/export")
def export_cashflows_report(
    client_id: int = Query(..., ge=1),
    period_start: str = Query(...),
    period_end: str = Query(...),
    group_by: str = Query("month"),
    search: str = Query('', max_length=200),
    category: str = Query('', max_length=200),
    account_head: str = Query('', max_length=300),
    treasury_type: str = Query('', max_length=40),
    period_key: str = Query('', max_length=80),
    treasury_view: str = Query('combined'),
    summary_filter: str = Query('', max_length=200),
    hide_zero_rows: bool = Query(False),
    session=Depends(require_admin),
):
    """Export the same Cashflows period and grouping shown in Reports."""
    # Export controls and independent ledger evidence use the same snapshot as
    # the source movements, exactly as the screen's balance-enabled preview.
    result = run_read_snapshot_pg(lambda _conn: _build_cashflow_result(
        client_id, period_start, period_end,
        detail_sections={'transfers', 'customer_payments', 'vendor_payments', 'deductions', 'review', 'drilldown'},
        group_by=group_by, treasury_view=treasury_view, include_balances=True,
    ))
    if not result["source"]["ready"] or not result["preview"].get("periods"):
        raise HTTPException(
            409,
            detail={
                "message": "The Cashflows report is not ready for export.",
                "blocking_reasons": result["blocking_reasons"],
            },
        )
    search = search.strip() if isinstance(search, str) else ""
    category = category.strip() if isinstance(category, str) else ""
    account_head = account_head.strip() if isinstance(account_head, str) else ""
    treasury_type = treasury_type.strip() if isinstance(treasury_type, str) else ""
    period_key = period_key.strip() if isinstance(period_key, str) else ""
    summary_filter = summary_filter.strip() if isinstance(summary_filter, str) else ""
    export_preview = dict(result["preview"])
    treasury_view = result.get("treasury_view") or normalize_cashflow_treasury_view(treasury_view)
    source_metadata = (result.get("source") or {}).get("metadata") or {}
    if not treasury_type and treasury_view in {'bank', 'cash'}:
        treasury_type = treasury_view
    period_keys = [
        str(item.get("key") or "")
        for item in (export_preview.get("periods") or [])
        if isinstance(item, dict) and item.get("key")
    ]
    source_totals = export_preview.get("treasury_view_totals") or export_preview.get("category_totals") or []
    if treasury_view == "split":
        visible_categories = _filter_cashflow_treasury_totals(
            source_totals,
            summary_filter=summary_filter,
            hide_zero_rows=hide_zero_rows,
        )
    else:
        visible_categories = _filter_cashflow_category_totals(
            source_totals,
            period_keys,
            summary_filter=summary_filter,
            hide_zero_rows=hide_zero_rows,
        )
    export_preview["treasury_view_totals"] = visible_categories
    if treasury_view != "split":
        export_preview["category_totals"] = visible_categories
    ledger_rows = export_preview.get("ledger_rollups") or []
    if summary_filter.strip() or hide_zero_rows:
        visible_category_names = {str(record.get("category") or "") for record in visible_categories}
        ledger_rows = [
            row for row in ledger_rows
            if str(row.get("category") or "") in visible_category_names
        ]
    export_preview["ledger_rollups"] = _filter_cashflow_ledger_rows(
        ledger_rows,
        search=search,
        category=category,
        account_head=account_head,
        treasury_type=treasury_type,
        period_key=period_key,
    )
    export_preview["export_filters"] = {
        "summary_filter": summary_filter.strip(),
        "hide_zero_rows": bool(hide_zero_rows),
        "search": search.strip(),
        "category": category.strip(),
        "account_head": account_head.strip(),
        "treasury_type": treasury_type.strip(),
        "period_key": period_key.strip(),
        "treasury_view": treasury_view,
    }
    content = export_cashflow_xlsx(
        export_preview,
        period_start=result["period_start"],
        period_end=result["period_end"],
        group_by=result["group_by"],
    )
    view_suffix = "" if treasury_view == "combined" else f"_{treasury_view}"
    filename = f"Cashflows{view_suffix}_{result['group_by']}_{result['period_start']}_to_{result['period_end']}.xlsx"
    return StreamingResponse(
        BytesIO(content),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/api/reconciliation/cashflows/sources")
def list_cashflow_sources(
    client_id: int = Query(..., ge=1),
    session=Depends(require_admin),
):
    """Return selectable Client Data Bank upload/template sources for Cashflows."""
    _require_active_client(client_id)
    return _cashflow_source_selection_response(client_id, _cashflow_source_options(client_id))

@router.put("/api/reconciliation/cashflows/source")
def save_cashflow_source(
    payload: CashflowSourceSelectionUpdate,
    session=Depends(require_admin),
):
    """Persist exactly one Client Data Bank upload or template for Cashflows."""
    _require_active_client(payload.client_id)
    if (payload.upload_id is None) == (payload.template_id is None):
        raise HTTPException(400, "Select exactly one Client Data Bank upload or template")
    if payload.upload_id is not None:
        source_rows = execute_query_pg(
            """SELECT 1 FROM client_data
               WHERE client_id = %s AND upload_id = %s AND is_deleted = FALSE
               LIMIT 1""",
            (payload.client_id, payload.upload_id),
        )
    else:
        source_rows = execute_query_pg(
            """SELECT 1 FROM client_data
               WHERE client_id = %s AND template_id = %s AND is_deleted = FALSE
               LIMIT 1""",
            (payload.client_id, payload.template_id),
        )
    if not source_rows:
        raise HTTPException(404, "The selected Client Data Bank source was not found for this client")
    actor = (session or {}).get("username") or "Admin"
    transaction = pg_transaction() if pg_transaction is not None else nullcontext()
    with transaction:
        execute_write_pg(
            """INSERT INTO account_control_cashflow_settings
               (client_id, source_upload_id, source_template_id, updated_by, updated_at)
               VALUES (%s, %s, %s, %s, CURRENT_TIMESTAMP)
               ON CONFLICT (client_id) DO UPDATE SET
                 source_upload_id = EXCLUDED.source_upload_id,
                 source_template_id = EXCLUDED.source_template_id,
                 updated_by = EXCLUDED.updated_by,
                 updated_at = CURRENT_TIMESTAMP""",
            (payload.client_id, payload.upload_id, payload.template_id, actor),
        )
    invalidate_cashflow_summary_read_models(
        payload.client_id,
        reason="source_selection_updated",
        changed_by=actor,
        write_fn=execute_write_pg,
    )
    log_audit("Admin", "Update", "Cashflows", f"Selected source for client {payload.client_id}")
    return _cashflow_configuration(payload.client_id)


@router.get("/api/reconciliation/financial-statements/preview")
def preview_financial_statements(
    client_id: int = Query(..., ge=1),
    period_start: str = Query(...),
    period_end: str = Query(...),
    journal_upload_id: Optional[int] = Query(None, ge=1),
    journal_template_id: Optional[int] = Query(None, ge=1),
    session=Depends(require_admin),
):
    """Return read-only statement-category totals from Account Control mappings."""
    return _public_report_result(
        _build_financial_statement_result(
            client_id,
            period_start,
            period_end,
            journal_upload_id=journal_upload_id,
            journal_template_id=journal_template_id,
        )
    )


@router.get("/api/reconciliation/reports/preview")
def preview_reconciliation_report(
    client_id: int = Query(..., ge=1),
    group_label: str = Query(..., min_length=1),
    period_start: str = Query(...),
    period_end: str = Query(...),
    journal_upload_id: Optional[int] = Query(None, ge=1),
    journal_template_id: Optional[int] = Query(None, ge=1),
    session=Depends(require_admin),
):
    """Return report controls, diagnostics, and a compact preview without creating a file."""
    result = _build_report_result(
        client_id,
        group_label,
        period_start,
        period_end,
        journal_upload_id=journal_upload_id,
        journal_template_id=journal_template_id,
    )
    return _public_report_result(result)


@router.get("/api/reconciliation/reports/download")
def download_reconciliation_report(
    client_id: int = Query(..., ge=1),
    group_label: str = Query(..., min_length=1),
    period_start: str = Query(...),
    period_end: str = Query(...),
    journal_upload_id: Optional[int] = Query(None, ge=1),
    journal_template_id: Optional[int] = Query(None, ge=1),
    session=Depends(require_admin),
):
    """Generate the professional Excel report only when all accounting controls pass."""
    result = _build_report_result(
        client_id,
        group_label,
        period_start,
        period_end,
        journal_upload_id=journal_upload_id,
        journal_template_id=journal_template_id,
    )
    if not result["can_download"]:
        raise HTTPException(
            409,
            detail={
                "message": "The report is not ready for download. Resolve the listed accounting controls first.",
                "blocking_reasons": result["blocking_reasons"],
            },
        )

    content = export_reconciliation_xlsx(
        result["_summary"],
        report_label=result["group_label"],
        cross_group_check=result["_cross_group_check"],
        opening_balances=result["_opening_balances"],
    )
    safe_group = re.sub(r"[^A-Za-z0-9_-]+", "_", result["group_label"]).strip("_") or "Ledger"
    start_month = result["period_start"][:7]
    end_month = result["period_end"][:7]
    filename = f"{safe_group}_Monthly_Summary_{start_month}_to_{end_month}.xlsx"
    return StreamingResponse(
        BytesIO(content),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
