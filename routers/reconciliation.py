"""Backend configuration for ledger reconciliation account groups."""

from __future__ import annotations

from contextlib import nullcontext
from datetime import date, datetime
from io import BytesIO
import math
import re
from typing import Any, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
import pandas as pd
from pydantic import BaseModel, Field

from database import execute_query_pg, execute_write_pg, log_audit, pg_transaction
from routers.auth import require_admin
from services.ledger_group_summary import LedgerBalanceError, build_cashflow_summary, build_cross_group_check, build_financial_statement_summary, build_group_summary
from services.ledger_report_source import CASHFLOW_COLUMN_ROLE_LABELS, CASHFLOW_REQUIRED_COLUMN_ROLES, load_ledger_report_source
from services.reconciliation_balance_source import (
    load_reconciliation_balance_source,
    normalize_balance_month,
)
from services.reconciliation_xlsx import export_reconciliation_xlsx


router = APIRouter()


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
    mappings = execute_query_pg(
        """
        SELECT a.account_code, c.category_key, c.label
        FROM account_control_cashflow_category_assignments a
        JOIN account_control_cashflow_categories c
          ON c.id = a.category_id AND c.client_id = a.client_id AND c.is_active = TRUE
        WHERE a.client_id = %s
        ORDER BY a.account_code
        """,
        (client_id,),
    )
    treasury = execute_query_pg(
        """SELECT account_code, treasury_type
           FROM account_control_cashflow_treasury_accounts
           WHERE client_id = %s AND is_active = TRUE
           ORDER BY treasury_type, account_code""",
        (client_id,),
    )
    settings = execute_query_pg(
        """SELECT match_internal_transfers, receipt_presentation, source_upload_id, source_template_id
           FROM account_control_cashflow_settings WHERE client_id = %s""",
        (client_id,),
    )
    return {
        "category_mappings": {normalize_account_code(row["account_code"]): row.get("label") or "" for row in mappings},
        "cash_account_codes": [normalize_account_code(row["account_code"]) for row in treasury if row["treasury_type"] == "cash"],
        "source_upload_id": settings[0].get("source_upload_id") if settings else None,
        "source_template_id": settings[0].get("source_template_id") if settings else None,
        "bank_account_codes": [normalize_account_code(row["account_code"]) for row in treasury if row["treasury_type"] == "bank"],
        "match_internal_transfers": bool(settings[0].get("match_internal_transfers")) if settings else False,
        "receipt_presentation": (settings[0].get("receipt_presentation") or "net") if settings else "net",
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


def _cashflow_preview(summary: dict[str, Any] | None) -> dict[str, Any]:
    if summary is None:
        return {
            "months": [],
            "category_totals": [],
            "transfer_details": [],
            "deduction_details": [],
            "categories_needing_review": [],
            "selected_voucher_count": 0,
            "internal_transfer_count": 0,
            "category_review_count": 0,
            "receipt_presentation": "net",
        }
    review = summary.get("categories_needing_review")
    return {
        "months": [str(month) for month in summary.get("months", [])],
        "category_totals": _category_total_records(summary.get("category_totals")),
        "transfer_details": _frame_records(summary.get("transfer_details")),
        "deduction_details": _frame_records(summary.get("deduction_details")),
        "categories_needing_review": _frame_records(review),
        "selected_voucher_count": int(summary.get("selected_voucher_count", 0)),
        "internal_transfer_count": int(summary.get("internal_transfer_count", 0)),
        "category_review_count": int(len(review)) if isinstance(review, pd.DataFrame) else 0,
        "receipt_presentation": summary.get("receipt_presentation") or "net",
    }


def _build_cashflow_result(
    client_id: int,
    period_start: Any,
    period_end: Any,
) -> dict[str, Any]:
    _require_active_client(client_id)
    start, end = _parse_report_period(period_start, period_end)
    configuration = _cashflow_configuration(client_id)
    configured_template_id = configuration.get("source_template_id")
    diagnostics: list[dict[str, Any]] = []
    try:
        source = load_ledger_report_source(
            client_id,
            journal_template_id=configured_template_id,
            period_start=start,
            period_end=end,
            query_fn=execute_query_pg,
            account_category_source="cashflow",
            treasury_account_codes=[
                *configuration["cash_account_codes"],
                *configuration["bank_account_codes"],
            ],
        )
    except ValueError as error:
        raise HTTPException(400, str(error)) from error
    diagnostics.extend(source.diagnostics)
    if configured_template_id is None:
        diagnostics.append(_report_diagnostic("cashflow_template_not_configured", "error", "Configure and map the Transaction Report template in Account Control Centre before generating Cashflows."))
    if not configuration["cash_account_codes"] and not configuration["bank_account_codes"]:
        diagnostics.append(_report_diagnostic("no_treasury_accounts", "error", "Select at least one Cash or Bank account in Account Control Centre before generating Cashflows."))

    summary: dict[str, Any] | None = None
    if source.ready and not any(item.get("severity") == "error" for item in diagnostics):
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
            )
        except LedgerBalanceError as error:
            diagnostics.append(_report_diagnostic("unbalanced_cashflow_vouchers", "error", str(error), checks=_frame_records(error.checks, limit=20)))
        except ValueError as error:
            diagnostics.append(_report_diagnostic("cashflow_input_error", "error", str(error)))

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
    source_quality = {
        "source_row_count": int(source_metadata.get("source_row_count") or 0),
        "valid_row_count": int(source_metadata.get("valid_row_count") or len(source.journal)),
        "rejected_row_count": int(source_metadata.get("rejected_row_count") or 0),
        "layout_row_count": int(source_metadata.get("layout_row_count") or 0),
        "out_of_period_row_count": int(source_metadata.get("out_of_period_row_count") or 0),
        "rejected_rows_sample": source_metadata.get("rejected_rows_sample") or [],
    }
    return {
        "client_id": client_id,
        "period_start": start.date().isoformat(),
        "period_end": end.date().isoformat(),
        "configuration": {
            "cash_account_count": len(configuration["cash_account_codes"]),
            "bank_account_count": len(configuration["bank_account_codes"]),
            "source_template_id": configured_template_id,
            "match_internal_transfers": configuration["match_internal_transfers"],
            "receipt_presentation": configuration["receipt_presentation"],
        },
        "source": {"metadata": source_metadata, "quality": source_quality, "diagnostics": diagnostics, "ready": source.ready and not any(item.get("severity") == "error" for item in diagnostics)},
        "controls": controls,
        "blocking_reasons": blocking_reasons,
        "preview": _cashflow_preview(summary),
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


@router.get("/api/reconciliation/cashflows/preview")
def preview_cashflows(
    client_id: int = Query(..., ge=1),
    period_start: str = Query(...),
    period_end: str = Query(...),
    session=Depends(require_admin),
):
    """Return Cashflows using the Transaction Report template configured in Account Control Centre."""
    return _build_cashflow_result(
        client_id,
        period_start,
        period_end,
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
