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


def normalize_group_label(value: Any) -> str:
    """Trim and collapse whitespace while preserving the user's display casing."""
    return re.sub(r"\s+", " ", str(value or "").strip())


def normalize_account_code(value: Any) -> str:
    """Normalize transport whitespace without changing an accounting code's case."""
    return str(value or "").strip()


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
