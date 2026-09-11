"""Backend configuration for ledger reconciliation account groups."""

from __future__ import annotations

from contextlib import nullcontext
import re
from typing import Any, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from database import execute_query_pg, execute_write_pg, log_audit, pg_transaction
from routers.auth import require_admin


router = APIRouter()


class AccountGroupUpdate(BaseModel):
    group_label: str
    account_codes: List[str] = Field(default_factory=list)


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
