"""Client Account Set foundation for the Chart of Accounts Control Centre."""

from __future__ import annotations

import logging
import re
from typing import Any, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from database import execute_query_pg, execute_write_pg, log_audit, pg_transaction
from routers.auth import require_admin
from routers.trial_balance import find_latest_parsed_upload
from services.cash_bank_classification import (
    discover_historical_orphan_accounts_postgres,
    synchronize_treasury_accounts_postgres,
)
from services.cashflow_preparation import (
    cashflow_preparation_requires_recovery,
    enqueue_cashflow_preparation_job,
    mark_cashflow_preparation_job_failed,
    run_cashflow_preparation_job_postgres,
)
from services.ledger_report_source import invalidate_cashflow_summary_read_models, prepare_cashflow_summary_snapshot



router = APIRouter()
logger = logging.getLogger(__name__)

def _refresh_cashflow_snapshot(
    client_id: int,
    reason: str,
    *,
    category_account_codes: list[str] | None = None,
    category_ids: list[int] | None = None,
    treasury_account_codes: list[str] | None = None,
    preparation_job: dict[str, Any] | None = None,
    background_tasks: BackgroundTasks | None = None,
) -> dict[str, Any] | None:
    try:
        if preparation_job is None and (category_account_codes or category_ids):
            with pg_transaction() as conn:
                preparation_job = enqueue_cashflow_preparation_job(
                    conn,
                    client_id=client_id,
                    reason=reason,
                    affected_account_codes=category_account_codes or (),
                    category_ids=category_ids or (),
                )
        elif preparation_job is None and treasury_account_codes:
            with pg_transaction() as conn:
                preparation_job = enqueue_cashflow_preparation_job(
                    conn,
                    client_id=client_id,
                    reason=reason,
                    affected_account_codes=treasury_account_codes,
                )
        if preparation_job is None and cashflow_preparation_requires_recovery(
            execute_query_pg,
            client_id=client_id,
        ):
            with pg_transaction() as conn:
                preparation_job = enqueue_cashflow_preparation_job(
                    conn,
                    client_id=client_id,
                    reason="preparation_recovery",
                )
        invalidate_cashflow_summary_read_models(client_id, reason=reason, write_fn=execute_write_pg)
        if preparation_job and background_tasks is not None:
            background_tasks.add_task(
                _run_cashflow_preparation_job_background,
                preparation_job["job_id"],
                client_id,
                reason,
            )
            prepare_cashflow_summary_snapshot(client_id)
        elif preparation_job:
            _run_cashflow_preparation_job_background(preparation_job["job_id"], client_id, reason)
        else:
            prepare_cashflow_summary_snapshot(client_id)
        return preparation_job
    except Exception as error:
        logger.warning("cashflows_snapshot_preparation_failed client_id=%s reason=%s error=%s", client_id, reason, error)
        return None


def _cashflow_treasury_change_codes(
    client_id: int,
    cash_account_codes: set[str],
    bank_account_codes: set[str],
) -> list[str]:
    rows = execute_query_pg(
        """SELECT account_code, treasury_type
           FROM account_control_cashflow_treasury_accounts
           WHERE client_id = %s AND is_active = TRUE""",
        (client_id,),
    )
    current_pairs = {
        (str(row["account_code"]), str(row["treasury_type"]))
        for row in rows
    }
    requested_pairs = {
        (str(code), "cash") for code in cash_account_codes
    } | {
        (str(code), "bank") for code in bank_account_codes
    }
    return sorted({
        str(account_code).strip()
        for account_code, _treasury_type in current_pairs ^ requested_pairs
        if str(account_code).strip()
    })


def _run_cashflow_preparation_job_background(job_id: int, client_id: int, reason: str) -> None:
    result = run_cashflow_preparation_job_postgres(job_id)
    logger.info(
        "cashflows_preparation_job client_id=%s reason=%s job_id=%s result=%s",
        client_id,
        reason,
        job_id,
        result,
    )
    try:
        prepare_cashflow_summary_snapshot(client_id)
    except Exception as error:
        logger.warning(
            "cashflows_snapshot_preparation_failed client_id=%s reason=%s job_id=%s error=%s",
            client_id,
            reason,
            job_id,
            error,
        )

_SELECTOR_TYPES = {
    "include_subtree",
    "include_exact",
    "exclude_subtree",
    "exclude_exact",
}


class AccountSetSelectorPayload(BaseModel):
    selector_type: str
    account_code: str
    sort_order: Optional[int] = Field(default=None, ge=0)


class AccountSetCreatePayload(BaseModel):
    client_id: int = Field(..., ge=1)
    set_key: str
    display_name: str
    description: Optional[str] = None
    selectors: list[AccountSetSelectorPayload] = Field(default_factory=list)


class AccountSetDraftUpdatePayload(BaseModel):
    display_name: str
    description: Optional[str] = None
    source_upload_id: Optional[int] = Field(default=None, ge=1)
    selectors: list[AccountSetSelectorPayload] = Field(default_factory=list)


def _normalise_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip())


def _normalise_key(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "_", _normalise_text(value).casefold()).strip("_")


def _normalise_account_code(value: Any) -> str:
    return str(value or "").strip()


def _actor_name(session: Any) -> str:
    if isinstance(session, dict):
        return str(session.get("username") or "Admin")
    return "Admin"


def _require_active_client(client_id: int) -> None:
    rows = execute_query_pg(
        "SELECT id FROM clients WHERE id = %s AND is_active = TRUE",
        (client_id,),
    )
    if not rows:
        raise HTTPException(404, "Client not found or inactive")


def _clean_set_identity(set_key: str, display_name: str, description: Optional[str]) -> tuple[str, str, Optional[str]]:
    key = _normalise_key(set_key)
    name = _normalise_text(display_name)
    cleaned_description = _normalise_text(description) or None
    if not key:
        raise HTTPException(400, "Account Set key is required")
    if len(key) > 80:
        raise HTTPException(400, "Account Set key must be 80 characters or fewer")
    if not name:
        raise HTTPException(400, "Account Set name is required")
    if len(name) > 120:
        raise HTTPException(400, "Account Set name must be 120 characters or fewer")
    if cleaned_description and len(cleaned_description) > 500:
        raise HTTPException(400, "Account Set description must be 500 characters or fewer")
    return key, name, cleaned_description


def _clean_draft_payload(payload: AccountSetDraftUpdatePayload) -> tuple[str, Optional[str], list[dict[str, Any]]]:
    name = _normalise_text(payload.display_name)
    description = _normalise_text(payload.description) or None
    if not name:
        raise HTTPException(400, "Account Set name is required")
    if len(name) > 120:
        raise HTTPException(400, "Account Set name must be 120 characters or fewer")
    if description and len(description) > 500:
        raise HTTPException(400, "Account Set description must be 500 characters or fewer")
    return name, description, _clean_selectors(payload.selectors)


def _clean_selectors(items: list[AccountSetSelectorPayload]) -> list[dict[str, Any]]:
    cleaned: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for position, item in enumerate(items):
        selector_type = _normalise_text(item.selector_type).casefold()
        account_code = _normalise_account_code(item.account_code)
        if selector_type not in _SELECTOR_TYPES:
            raise HTTPException(400, "Account Set selector type is invalid")
        if not account_code:
            raise HTTPException(400, "Account Set selector account code cannot be blank")
        if len(account_code) > 120:
            raise HTTPException(400, "Account Set selector account code must be 120 characters or fewer")
        key = (selector_type, account_code)
        if key in seen:
            raise HTTPException(400, f"Duplicate selector for {account_code}")
        seen.add(key)
        cleaned.append(
            {
                "selector_type": selector_type,
                "account_code": account_code,
                "sort_order": item.sort_order if item.sort_order is not None else position,
            }
        )
    return cleaned


def _account_code_is_within(account_code: str, root_code: str) -> bool:
    """Match a code or a complete dash-delimited descendant, never a partial prefix."""
    return account_code == root_code or account_code.startswith(f"{root_code}-")


def _selector_matches(account_code: str, selector: dict[str, Any]) -> bool:
    if selector["selector_type"].endswith("exact"):
        return account_code == selector["account_code"]
    return _account_code_is_within(account_code, selector["account_code"])


def _resolve_leaf_accounts(accounts: list[dict[str, Any]], selectors: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Resolve selector rules against supplied Trial Balance rows without mutating them."""
    includes = [item for item in selectors if item["selector_type"].startswith("include")]
    excludes = [item for item in selectors if item["selector_type"].startswith("exclude")]
    resolved: list[dict[str, Any]] = []
    for account in accounts:
        if account.get("classification") != "leaf":
            continue
        account_code = _normalise_account_code(account.get("account_code"))
        matched_by = [item for item in includes if _selector_matches(account_code, item)]
        if not matched_by or any(_selector_matches(account_code, item) for item in excludes):
            continue
        resolved.append(
            {
                "id": account.get("id"),
                "account_code": account_code,
                "account_name": account.get("account_name"),
                "parent_account_code": account.get("parent_account_code"),
                "level": account.get("level"),
                "matched_by": [
                    {"selector_type": item["selector_type"], "account_code": item["account_code"]}
                    for item in matched_by
                ],
            }
        )
    return resolved



def get_published_account_set_leaf_accounts(client_id: int, set_key: str) -> Optional[dict[str, Any]]:
    """Return one client's published Account Set leaf accounts, or None when absent."""
    normalised_key = _normalise_key(set_key)
    rows = execute_query_pg(
        """
        SELECT s.id AS account_set_id, s.set_key, v.id AS version_id, v.version_number,
               v.source_upload_id, v.display_name
        FROM account_sets s
        JOIN account_set_versions v ON v.id = s.active_version_id
        WHERE s.client_id = %s AND s.set_key = %s AND s.is_active = TRUE
          AND v.status = 'published'
        """,
        (client_id, normalised_key),
    )
    if not rows:
        return None
    published = rows[0]
    resolved = _resolve_leaf_accounts(
        _accounts_for_upload(published["source_upload_id"]),
        _version_selectors(published["version_id"]),
    )
    return {
        "account_set_id": published["account_set_id"],
        "set_key": published["set_key"],
        "version_id": published["version_id"],
        "version_number": published["version_number"],
        "display_name": published["display_name"],
        "accounts": resolved,
    }
def _accounts_for_upload(upload_id: int) -> list[dict[str, Any]]:
    return execute_query_pg(
        """
        SELECT id, account_code, account_name, parent_account_code, level, classification, sort_order
        FROM trial_balance_accounts
        WHERE upload_id = %s
        ORDER BY sort_order, account_code
        """,
        (upload_id,),
    )


def _require_client_upload(client_id: int, upload_id: int) -> dict[str, Any]:
    rows = execute_query_pg(
        """
        SELECT id, client_id, as_of_date, period_start, period_end, period_type, uploaded_at
        FROM trial_balance_uploads
        WHERE id = %s AND client_id = %s AND status = 'parsed'
        """,
        (upload_id, client_id),
    )
    if not rows:
        raise HTTPException(400, "A parsed Trial Balance for this client is required")
    return rows[0]


def _validate_selectors_against_upload(
    client_id: int,
    upload_id: int,
    selectors: list[dict[str, Any]],
    require_resolved_accounts: bool = False,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    upload = _require_client_upload(client_id, upload_id)
    accounts = _accounts_for_upload(upload_id)
    by_code = {_normalise_account_code(row["account_code"]): row for row in accounts}
    missing_codes = sorted({item["account_code"] for item in selectors if item["account_code"] not in by_code})
    if missing_codes:
        raise HTTPException(
            400,
            detail={
                "message": "One or more selector codes are not present in the selected Trial Balance",
                "account_codes": missing_codes,
            },
        )
    invalid_exact = sorted(
        item["account_code"]
        for item in selectors
        if item["selector_type"].endswith("exact") and by_code[item["account_code"]]["classification"] != "leaf"
    )
    if invalid_exact:
        raise HTTPException(
            400,
            detail={
                "message": "Exact selectors must identify leaf accounts",
                "account_codes": invalid_exact,
            },
        )
    resolved = _resolve_leaf_accounts(accounts, selectors)
    if require_resolved_accounts and not resolved:
        raise HTTPException(400, "A published Account Set must resolve to at least one leaf account")
    return upload, resolved


def _get_set(account_set_id: int) -> dict[str, Any]:
    rows = execute_query_pg(
        """
        SELECT id, client_id, set_key, active_version_id, is_active, created_at, updated_at
        FROM account_sets
        WHERE id = %s AND is_active = TRUE
        """,
        (account_set_id,),
    )
    if not rows:
        raise HTTPException(404, "Account Set not found or inactive")
    return rows[0]


def _get_version(version_id: int) -> dict[str, Any]:
    rows = execute_query_pg(
        """
        SELECT v.id, v.account_set_id, v.version_number, v.source_upload_id,
               v.display_name, v.description, v.status, v.created_at, v.published_at,
               s.client_id, s.set_key, s.active_version_id
        FROM account_set_versions v
        JOIN account_sets s ON s.id = v.account_set_id
        WHERE v.id = %s AND s.is_active = TRUE
        """,
        (version_id,),
    )
    if not rows:
        raise HTTPException(404, "Account Set version not found")
    return rows[0]


def _version_selectors(version_id: int) -> list[dict[str, Any]]:
    return execute_query_pg(
        """
        SELECT selector_type, account_code, sort_order
        FROM account_set_selectors
        WHERE account_set_version_id = %s
        ORDER BY sort_order, id
        """,
        (version_id,),
    )


def _replace_selectors(version_id: int, selectors: list[dict[str, Any]]) -> None:
    execute_write_pg("DELETE FROM account_set_selectors WHERE account_set_version_id = %s", (version_id,))
    for selector in selectors:
        execute_write_pg(
            """
            INSERT INTO account_set_selectors (account_set_version_id, selector_type, account_code, sort_order)
            VALUES (%s, %s, %s, %s)
            """,
            (version_id, selector["selector_type"], selector["account_code"], selector["sort_order"]),
        )


def _serialise_version(version: dict[str, Any], include_selectors: bool = True) -> dict[str, Any]:
    result = {
        "id": version["id"],
        "account_set_id": version["account_set_id"],
        "version_number": version["version_number"],
        "source_upload_id": version["source_upload_id"],
        "display_name": version["display_name"],
        "description": version["description"],
        "status": version["status"],
        "created_at": version["created_at"],
        "published_at": version["published_at"],
    }
    if include_selectors:
        result["selectors"] = _version_selectors(version["id"])
    return result


def _serialise_set(account_set_id: int) -> dict[str, Any]:
    account_set = _get_set(account_set_id)
    versions = execute_query_pg(
        """
        SELECT id, account_set_id, version_number, source_upload_id, display_name,
               description, status, created_at, published_at
        FROM account_set_versions
        WHERE account_set_id = %s
        ORDER BY version_number DESC
        """,
        (account_set_id,),
    )
    return {
        "id": account_set["id"],
        "client_id": account_set["client_id"],
        "set_key": account_set["set_key"],
        "active_version_id": account_set["active_version_id"],
        "is_active": bool(account_set["is_active"]),
        "created_at": account_set["created_at"],
        "updated_at": account_set["updated_at"],
        "versions": [_serialise_version(version) for version in versions],
    }


@router.get("/api/account-control-center/accounts/latest")
def latest_account_tree(client_id: int = Query(..., ge=1), session=Depends(require_admin)):
    """Return the authoritative latest parsed Trial Balance tree for one client."""
    _require_active_client(client_id)
    upload = find_latest_parsed_upload(client_id)
    if not upload:
        raise HTTPException(404, "No parsed Trial Balance found for this client")
    accounts = _accounts_for_upload(upload["id"])
    return {
        "client_id": client_id,
        "upload": upload,
        "accounts": [
            {
                "id": row["id"],
                "account_code": row["account_code"],
                "account_name": row["account_name"],
                "parent_account_code": row["parent_account_code"],
                "level": row["level"],
                "is_leaf": row["classification"] == "leaf",
            }
            for row in accounts
        ],
    }



_VERIFICATION_WORKSTREAMS = frozenset({
    "cash", "bank", "local_vendor", "import_vendor", "export_customer", "payroll", "contractor",
})


class AccountControlAssignmentPayload(BaseModel):
    client_id: int = Field(..., ge=1)
    account_code: str
    verification_workstream: Optional[str] = None
    financial_statement_category_id: Optional[int] = Field(default=None, ge=1)
    note_category_id: Optional[int] = Field(default=None, ge=1)


class AccountControlBulkAssignmentPayload(BaseModel):
    client_id: int = Field(..., ge=1)
    parent_account_code: str
    verification_workstream: Optional[str] = None
    financial_statement_category_id: Optional[int] = Field(default=None, ge=1)
    note_category_id: Optional[int] = Field(default=None, ge=1)


class AccountControlCategoryPayload(BaseModel):
    client_id: int = Field(..., ge=1)
    category_type: str = "financial_statement"
    category_key: str
    label: str
    statement_section: Optional[str] = None
    row_type: str = "category"
    parent_category_id: Optional[int] = Field(default=None, ge=1)
    sort_order: int = Field(default=0, ge=0)
    is_active: bool = True


class AccountControlCategoryUpdatePayload(BaseModel):
    client_id: int = Field(..., ge=1)
    category_type: str = "financial_statement"
    label: str
    statement_section: Optional[str] = None
    row_type: str = "category"
    parent_category_id: Optional[int] = Field(default=None, ge=1)
    sort_order: int = Field(default=0, ge=0)
    is_active: bool = True


_ACCOUNT_CONTROL_ASSIGNMENT_FIELDS = (
    "verification_workstream", "financial_statement_category_id", "note_category_id",
)
_ACCOUNT_CONTROL_CATEGORY_TYPES = frozenset({"financial_statement", "note"})
_CASHFLOW_SECTIONS = frozenset({"unassigned", "money_in", "money_out", "internal_transfer", "excluded"})
_CASHFLOW_ROLES = frozenset({"none", "cash", "bank", "both"})
_CASHFLOW_TREATMENTS = frozenset({
    "standard", "customer_receipt", "withholding_tax", "bank_charge", "other_deduction", "non_cash",
})
_CASHFLOW_ROLE_CATEGORY_KEYS = (
    "customer_receipt",
    "bank_charge",
    "withholding_tax",
    "other_deduction",
)


class CashFlowAssignmentPayload(BaseModel):
    client_id: int = Field(..., ge=1)
    account_code: str
    section: str = "unassigned"
    category_label: Optional[str] = None
    cash_bank_role: str = "none"
    treatment_type: str = "standard"


class CashFlowBulkAssignmentPayload(BaseModel):
    client_id: int = Field(..., ge=1)
    parent_account_code: str
    section: str = "unassigned"
    category_label: Optional[str] = None
    cash_bank_role: str = "none"
    treatment_type: str = "standard"


class CashFlowCategoryPayload(BaseModel):
    client_id: int = Field(..., ge=1)
    category_key: str
    label: str
    sort_order: int = Field(default=0, ge=0)
    is_active: bool = True


class CashFlowCategoryUpdatePayload(BaseModel):
    client_id: int = Field(..., ge=1)
    label: str
    sort_order: int = Field(default=0, ge=0)
    is_active: bool = True


class CashFlowCategoryAssignmentPayload(BaseModel):
    client_id: int = Field(..., ge=1)
    account_code: str
    category_id: Optional[int] = Field(default=None, ge=1)


class CashFlowCategoryBulkAssignmentPayload(BaseModel):
    client_id: int = Field(..., ge=1)
    parent_account_code: str
    category_id: Optional[int] = Field(default=None, ge=1)


class CashFlowTreasuryAccountsPayload(BaseModel):
    client_id: int = Field(..., ge=1)
    cash_account_codes: list[str] = Field(default_factory=list)
    bank_account_codes: list[str] = Field(default_factory=list)


class CashFlowSettingsPayload(BaseModel):
    client_id: int = Field(..., ge=1)
    match_internal_transfers: bool = False
    receipt_presentation: str = "net"
    customer_receipt_category_ids: list[int] = Field(default_factory=list)
    bank_charge_category_ids: list[int] = Field(default_factory=list)
    withholding_tax_category_ids: list[int] = Field(default_factory=list)
    other_deduction_category_ids: list[int] = Field(default_factory=list)


def _clean_control_account_code(value: Any) -> str:
    code = _normalise_account_code(value)
    if not code or len(code) > 120:
        raise HTTPException(400, "Account code is required and must be 120 characters or fewer")
    return code


def _clean_cashflow_assignment(payload: CashFlowAssignmentPayload) -> dict[str, Any]:
    account_code = _clean_control_account_code(payload.account_code)
    section = _normalise_key(payload.section) or "unassigned"
    category_label = _normalise_text(payload.category_label) or None
    role = _normalise_key(payload.cash_bank_role) or "none"
    treatment = _normalise_key(payload.treatment_type) or "standard"
    if section not in _CASHFLOW_SECTIONS:
        raise HTTPException(400, "Cash Flow section is invalid")
    if role not in _CASHFLOW_ROLES:
        raise HTTPException(400, "Cash/Bank role is invalid")
    if treatment not in _CASHFLOW_TREATMENTS:
        raise HTTPException(400, "Cash Flow treatment type is invalid")
    if category_label and len(category_label) > 120:
        raise HTTPException(400, "Cash Flow category label must be 120 characters or fewer")
    if section in {"money_in", "money_out", "internal_transfer"} and not category_label:
        raise HTTPException(400, "A report category is required for an included Cash Flow account")
    return {
        "client_id": payload.client_id,
        "account_code": account_code,
        "section": section,
        "category_label": category_label,
        "cash_bank_role": role,
        "treatment_type": treatment,
    }


def _public_cashflow_assignment(assignment: dict[str, Any]) -> dict[str, Any]:
    return {
        "client_id": assignment["client_id"],
        "account_code": assignment["account_code"],
        "section": assignment.get("section") or "unassigned",
        "category_label": assignment.get("category_label"),
        "cash_bank_role": assignment.get("cash_bank_role") or "none",
        "treatment_type": assignment.get("treatment_type") or "standard",
    }


def _upsert_cashflow_assignment(assignment: dict[str, Any], updated_by: str) -> None:
    execute_write_pg(
        """
        INSERT INTO account_control_cashflow_assignments
            (client_id, account_code, section, category_label, cash_bank_role, treatment_type, updated_by, updated_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, CURRENT_TIMESTAMP)
        ON CONFLICT (client_id, account_code) DO UPDATE SET
            section = EXCLUDED.section,
            category_label = EXCLUDED.category_label,
            cash_bank_role = EXCLUDED.cash_bank_role,
            treatment_type = EXCLUDED.treatment_type,
            updated_by = EXCLUDED.updated_by,
            updated_at = CURRENT_TIMESTAMP
        """,
        (
            assignment["client_id"],
            assignment["account_code"],
            assignment["section"],
            assignment["category_label"],
            assignment["cash_bank_role"],
            assignment["treatment_type"],
            updated_by,
        ),
    )


def _clean_cashflow_category_payload(payload: CashFlowCategoryPayload) -> dict[str, Any]:
    category_key = _normalise_key(payload.category_key)
    label = _normalise_text(payload.label)
    if not category_key or len(category_key) > 80:
        raise HTTPException(400, "Cash Flow category key is required and must be 80 characters or fewer")
    if not label or len(label) > 120:
        raise HTTPException(400, "Cash Flow category label is required and must be 120 characters or fewer")
    return {
        "client_id": payload.client_id,
        "category_key": category_key,
        "label": label,
        "sort_order": payload.sort_order,
        "is_active": payload.is_active,
    }


def _clean_cashflow_category_update(payload: CashFlowCategoryUpdatePayload) -> dict[str, Any]:
    label = _normalise_text(payload.label)
    if not label or len(label) > 120:
        raise HTTPException(400, "Cash Flow category label is required and must be 120 characters or fewer")
    return {
        "client_id": payload.client_id,
        "label": label,
        "sort_order": payload.sort_order,
        "is_active": payload.is_active,
    }


def _clean_cashflow_category_assignment(payload: CashFlowCategoryAssignmentPayload) -> dict[str, Any]:
    return {
        "client_id": payload.client_id,
        "account_code": _clean_control_account_code(payload.account_code),
        "category_id": payload.category_id,
    }


def _validate_cashflow_category(client_id: int, category_id: Optional[int]) -> None:
    if category_id is None:
        return
    rows = execute_query_pg(
        "SELECT id FROM account_control_cashflow_categories WHERE id = %s AND client_id = %s AND is_active = TRUE",
        (category_id, client_id),
    )
    if not rows:
        raise HTTPException(400, "Cash Flow category is invalid or inactive")


def _clean_cashflow_role_categories(payload: CashFlowSettingsPayload) -> dict[str, list[int]]:
    values = {
        "customer_receipt": payload.customer_receipt_category_ids,
        "bank_charge": payload.bank_charge_category_ids,
        "withholding_tax": payload.withholding_tax_category_ids,
        "other_deduction": payload.other_deduction_category_ids,
    }
    return {
        role: sorted({int(category_id) for category_id in category_ids if int(category_id) > 0})
        for role, category_ids in values.items()
    }


def _upsert_cashflow_category_assignment(assignment: dict[str, Any], updated_by: str) -> None:
    execute_write_pg(
        """
        INSERT INTO account_control_cashflow_category_assignments
            (client_id, account_code, category_id, updated_by, updated_at)
        VALUES (%s, %s, %s, %s, CURRENT_TIMESTAMP)
        ON CONFLICT (client_id, account_code) DO UPDATE SET
            category_id = EXCLUDED.category_id,
            updated_by = EXCLUDED.updated_by,
            updated_at = CURRENT_TIMESTAMP
        """,
        (assignment["client_id"], assignment["account_code"], assignment["category_id"], updated_by),
    )


def _clean_control_assignment(payload: AccountControlAssignmentPayload) -> dict[str, Any]:
    account_code = _clean_control_account_code(payload.account_code)
    workstream = _normalise_text(payload.verification_workstream).casefold() or None
    if workstream and workstream not in _VERIFICATION_WORKSTREAMS:
        raise HTTPException(400, "Verification workstream is invalid")
    provided = set(getattr(payload, "model_fields_set", set()))
    return {
        "client_id": payload.client_id,
        "account_code": account_code,
        "verification_workstream": workstream,
        "financial_statement_category_id": payload.financial_statement_category_id,
        "note_category_id": payload.note_category_id,
        "_provided_fields": provided.intersection(_ACCOUNT_CONTROL_ASSIGNMENT_FIELDS),
    }


def _latest_control_accounts(client_id: int) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    _require_active_client(client_id)
    upload = find_latest_parsed_upload(client_id)
    if not upload:
        raise HTTPException(404, "No parsed Trial Balance found for this client")
    return upload, _accounts_for_upload(upload["id"])


def _validate_control_categories(client_id: int, assignment: dict[str, Any]) -> None:
    checks = (
        (assignment["financial_statement_category_id"], "financial_statement"),
        (assignment["note_category_id"], "note"),
    )
    for category_id, category_type in checks:
        if category_id is None:
            continue
        rows = execute_query_pg(
            """SELECT id, row_type FROM account_control_categories
               WHERE id = %s AND client_id = %s AND category_type = %s AND is_active = TRUE""",
            (category_id, client_id, category_type),
        )
        if not rows:
            raise HTTPException(400, f"Invalid active {category_type.replace('_', ' ')} category")
        if category_type == "financial_statement" and rows[0].get("row_type", "category") != "category":
            raise HTTPException(400, "Headings cannot be assigned to accounts")


def _upsert_control_assignment(assignment: dict[str, Any], updated_by: str) -> None:
    """Upsert only fields supplied by the active tab; other tab assignments remain intact."""
    provided = [field for field in _ACCOUNT_CONTROL_ASSIGNMENT_FIELDS if field in assignment.get("_provided_fields", set())]
    columns = ["client_id", "account_code"] + provided + ["updated_by", "updated_at"]
    values = [assignment["client_id"], assignment["account_code"]] + [assignment[field] for field in provided] + [updated_by]
    placeholders = ["%s"] * (len(values)) + ["CURRENT_TIMESTAMP"]
    if provided:
        updates = [f"{field} = EXCLUDED.{field}" for field in provided]
        updates.extend(["updated_by = EXCLUDED.updated_by", "updated_at = CURRENT_TIMESTAMP"])
        conflict = "DO UPDATE SET " + ", ".join(updates)
    else:
        conflict = "DO NOTHING"
    execute_write_pg(
        f"INSERT INTO account_control_assignments ({', '.join(columns)}) VALUES ({', '.join(placeholders)}) ON CONFLICT (client_id, account_code) {conflict}",
        tuple(values),
    )


def _clean_category_type(value: str) -> str:
    category_type = _normalise_text(value).casefold()
    if category_type not in _ACCOUNT_CONTROL_CATEGORY_TYPES:
        raise HTTPException(400, "Category type is invalid")
    return category_type


def _clean_category_payload(payload: AccountControlCategoryPayload) -> dict[str, Any]:
    category_type = _clean_category_type(payload.category_type)
    category_key = _normalise_key(payload.category_key)
    label = _normalise_text(payload.label)
    if not category_key or len(category_key) > 80:
        raise HTTPException(400, "Category key is required and must be 80 characters or fewer")
    if not label or len(label) > 120:
        raise HTTPException(400, "Category label is required and must be 120 characters or fewer")
    statement_section = _clean_statement_section(payload.statement_section)
    row_type = _clean_statement_row_type(payload.row_type)
    if category_type == "financial_statement" and row_type == "heading" and payload.parent_category_id is not None:
        raise HTTPException(400, "Headings must be top-level")
    return {
        "client_id": payload.client_id,
        "category_type": category_type,
        "category_key": category_key,
        "label": label,
        "statement_section": statement_section,
        "row_type": row_type,
        "parent_category_id": payload.parent_category_id,
        "sort_order": payload.sort_order,
        "is_active": payload.is_active,
    }


def _clean_category_update(payload: AccountControlCategoryUpdatePayload) -> dict[str, Any]:
    category_type = _clean_category_type(payload.category_type)
    label = _normalise_text(payload.label)
    if not label or len(label) > 120:
        raise HTTPException(400, "Category label is required and must be 120 characters or fewer")
    statement_section = _clean_statement_section(payload.statement_section)
    row_type = _clean_statement_row_type(payload.row_type)
    if category_type == "financial_statement" and row_type == "heading" and payload.parent_category_id is not None:
        raise HTTPException(400, "Headings must be top-level")
    return {
        "client_id": payload.client_id,
        "category_type": category_type,
        "label": label,
        "statement_section": statement_section,
        "row_type": row_type,
        "parent_category_id": payload.parent_category_id,
        "sort_order": payload.sort_order,
        "is_active": payload.is_active,
    }


def _clean_statement_section(value: Optional[str]) -> Optional[str]:
    if value is None or not _normalise_text(value):
        return None
    section = _normalise_text(value).casefold()
    if section not in {"balance_sheet", "income_statement"}:
        raise HTTPException(400, "Financial statement section is invalid")
    return section


def _clean_statement_row_type(value: str) -> str:
    row_type = _normalise_text(value).casefold()
    if row_type not in {"heading", "category"}:
        raise HTTPException(400, "Financial statement row type is invalid")
    return row_type


def _validate_statement_parent(category: dict[str, Any], current_id: Optional[int] = None) -> None:
    if category["category_type"] != "financial_statement":
        if category["statement_section"] or category["parent_category_id"] is not None:
            raise HTTPException(400, "Only Financial Statement categories can use outline sections")
        return
    parent_id = category["parent_category_id"]
    if category["row_type"] == "heading" and parent_id is not None:
        raise HTTPException(400, "Headings must be top-level")
    if parent_id is None:
        return
    if not category["statement_section"]:
        raise HTTPException(400, "Choose a statement before nesting this category")
    if parent_id == current_id:
        raise HTTPException(400, "A category cannot be its own parent")
    parents = execute_query_pg(
        "SELECT statement_section, row_type, is_active FROM account_control_categories WHERE id = %s AND client_id = %s AND category_type = 'financial_statement'",
        (parent_id, category["client_id"]),
    )
    if not parents or parents[0]["row_type"] != "heading" or not parents[0]["is_active"] or parents[0]["statement_section"] != category["statement_section"]:
        raise HTTPException(400, "Choose an active heading in the same statement")


def _public_control_assignment(assignment: dict[str, Any]) -> dict[str, Any]:
    return {field: assignment[field] for field in (
        "client_id", "account_code", "verification_workstream",
        "financial_statement_category_id", "note_category_id",
    )}


@router.get("/api/account-control-center/categories")
def list_account_control_categories(
    client_id: int = Query(..., ge=1),
    category_type: str = Query("financial_statement"),
    include_inactive: bool = Query(False),
    session=Depends(require_admin),
):
    _require_active_client(client_id)
    cleaned_type = _clean_category_type(category_type)
    active_clause = "" if include_inactive else " AND is_active = TRUE"
    rows = execute_query_pg(
        f"""SELECT id, client_id, category_type, category_key, label, statement_section, row_type,
                   parent_category_id, sort_order, is_active
            FROM account_control_categories
            WHERE client_id = %s AND category_type = %s{active_clause}
            ORDER BY sort_order, LOWER(label), category_key""",
        (client_id, cleaned_type),
    )
    return {"client_id": client_id, "category_type": cleaned_type, "categories": [dict(row) for row in rows]}


@router.post("/api/account-control-center/categories")
def create_account_control_category(payload: AccountControlCategoryPayload, session=Depends(require_admin)):
    category = _clean_category_payload(payload)
    _require_active_client(category["client_id"])
    _validate_statement_parent(category)
    existing = execute_query_pg(
        "SELECT id FROM account_control_categories WHERE client_id = %s AND category_type = %s AND category_key = %s",
        (category["client_id"], category["category_type"], category["category_key"]),
    )
    if existing:
        raise HTTPException(409, "A category with this key already exists")
    execute_write_pg(
        """INSERT INTO account_control_categories
           (client_id, category_type, category_key, label, statement_section, row_type, parent_category_id, sort_order, is_active, updated_at)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, CURRENT_TIMESTAMP)""",
        (category["client_id"], category["category_type"], category["category_key"], category["label"], category["statement_section"], category["row_type"], category["parent_category_id"], category["sort_order"], category["is_active"]),
    )
    return {"saved": True, "category": category}

@router.put("/api/account-control-center/categories/{category_id}")
def update_account_control_category(category_id: int, payload: AccountControlCategoryUpdatePayload, session=Depends(require_admin)):
    category = _clean_category_update(payload)
    rows = execute_query_pg(
        "SELECT id FROM account_control_categories WHERE id = %s AND client_id = %s AND category_type = %s",
        (category_id, category["client_id"], category["category_type"]),
    )
    if not rows:
        raise HTTPException(404, "Category not found")
    _validate_statement_parent(category)
    execute_write_pg(
        """UPDATE account_control_categories
           SET label = %s, statement_section = %s, row_type = %s, parent_category_id = %s,
               sort_order = %s, is_active = %s, updated_at = CURRENT_TIMESTAMP
           WHERE id = %s AND client_id = %s AND category_type = %s""",
        (category["label"], category["statement_section"], category["row_type"], category["parent_category_id"], category["sort_order"], category["is_active"], category_id, category["client_id"], category["category_type"]),
    )
    return {"saved": True, "category": {"id": category_id, **category}}

@router.get("/api/account-control-center/mapping")
def get_account_control_mapping(client_id: int = Query(..., ge=1), session=Depends(require_admin)):
    """Return the latest Trial Balance tree with one simplified assignment object per account."""
    upload, accounts = _latest_control_accounts(client_id)
    rows = execute_query_pg(
        """SELECT account_code, verification_workstream, financial_statement_category_id, note_category_id
           FROM account_control_assignments WHERE client_id = %s""",
        (client_id,),
    )
    assignments = {_normalise_account_code(row["account_code"]): row for row in rows}
    return {
        "client_id": client_id,
        "upload": upload,
        "verification_workstreams": sorted(_VERIFICATION_WORKSTREAMS),
        "accounts": [
            {
                "id": row["id"],
                "account_code": row["account_code"],
                "account_name": row["account_name"],
                "parent_account_code": row["parent_account_code"],
                "level": row["level"],
                "is_leaf": row["classification"] == "leaf",
                "assignment": {
                    "verification_workstream": assignments.get(_normalise_account_code(row["account_code"]), {}).get("verification_workstream"),
                    "financial_statement_category_id": assignments.get(_normalise_account_code(row["account_code"]), {}).get("financial_statement_category_id"),
                    "note_category_id": assignments.get(_normalise_account_code(row["account_code"]), {}).get("note_category_id"),
                },
            }
            for row in accounts
        ],
    }


@router.put("/api/account-control-center/mapping")
def save_account_control_mapping(payload: AccountControlAssignmentPayload, session=Depends(require_admin)):
    """Save one leaf-account assignment; parent-to-child application is a separate bulk operation."""
    assignment = _clean_control_assignment(payload)
    _upload, accounts = _latest_control_accounts(assignment["client_id"])
    account = next(
        (row for row in accounts if _normalise_account_code(row["account_code"]) == assignment["account_code"]),
        None,
    )
    if not account:
        raise HTTPException(400, "Account code is not present in the latest Trial Balance")
    if account["classification"] != "leaf":
        raise HTTPException(400, "Assign the parent through the bulk child operation; direct assignments require a leaf account")
    _validate_control_categories(assignment["client_id"], assignment)
    _upsert_control_assignment(assignment, _actor_name(session))
    return {"saved": True, "account_code": assignment["account_code"], "assignment": _public_control_assignment(assignment)}


@router.post("/api/account-control-center/mapping/apply-to-children")
def apply_account_control_mapping_to_children(payload: AccountControlBulkAssignmentPayload, session=Depends(require_admin)):
    """Apply one assignment to every latest-Trial-Balance leaf below a selected parent."""
    parent_code = _clean_control_account_code(payload.parent_account_code)
    internal_payload = {"client_id": payload.client_id, "account_code": parent_code}
    for field in _ACCOUNT_CONTROL_ASSIGNMENT_FIELDS:
        if field in getattr(payload, "model_fields_set", set()):
            internal_payload[field] = getattr(payload, field)
    assignment = _clean_control_assignment(AccountControlAssignmentPayload(**internal_payload))
    _upload, accounts = _latest_control_accounts(assignment["client_id"])
    parent = next(
        (row for row in accounts if _normalise_account_code(row["account_code"]) == parent_code),
        None,
    )
    if not parent:
        raise HTTPException(400, "Parent account is not present in the latest Trial Balance")
    leaves = [
        row for row in accounts
        if row["classification"] == "leaf"
        and (
            _normalise_account_code(row["account_code"]) == parent_code
            or _normalise_account_code(row["account_code"]).startswith(parent_code + "-")
        )
    ]
    if not leaves:
        raise HTTPException(400, "No leaf accounts were found below the selected account")
    _validate_control_categories(assignment["client_id"], assignment)
    actor = _actor_name(session)
    with pg_transaction():
        for row in leaves:
            child_assignment = {**assignment, "account_code": row["account_code"]}
            _upsert_control_assignment(child_assignment, actor)
    return {"saved": True, "parent_account_code": parent_code, "leaf_count": len(leaves)}

@router.get("/api/account-control-center/cashflow")
def get_account_control_cashflow(client_id: int = Query(..., ge=1), session=Depends(require_admin)):
    """Return the latest Trial Balance tree with separate Cash Flow Setup rules."""
    upload, accounts = _latest_control_accounts(client_id)
    rows = execute_query_pg(
        """SELECT account_code, section, category_label, cash_bank_role, treatment_type
           FROM account_control_cashflow_assignments WHERE client_id = %s""",
        (client_id,),
    )
    assignments = {_normalise_account_code(row["account_code"]): row for row in rows}
    return {
        "client_id": client_id,
        "upload": upload,
        "sections": sorted(_CASHFLOW_SECTIONS),
        "cash_bank_roles": sorted(_CASHFLOW_ROLES),
        "treatment_types": sorted(_CASHFLOW_TREATMENTS),
        "accounts": [
            {
                "id": row["id"],
                "account_code": row["account_code"],
                "account_name": row["account_name"],
                "parent_account_code": row["parent_account_code"],
                "level": row["level"],
                "is_leaf": row["classification"] == "leaf",
                "cashflow": {
                    "section": assignments.get(_normalise_account_code(row["account_code"]), {}).get("section") or "unassigned",
                    "category_label": assignments.get(_normalise_account_code(row["account_code"]), {}).get("category_label"),
                    "cash_bank_role": assignments.get(_normalise_account_code(row["account_code"]), {}).get("cash_bank_role") or "none",
                    "treatment_type": assignments.get(_normalise_account_code(row["account_code"]), {}).get("treatment_type") or "standard",
                },
            }
            for row in accounts
        ],
    }


@router.put("/api/account-control-center/cashflow")
def save_account_control_cashflow(payload: CashFlowAssignmentPayload, session=Depends(require_admin)):
    """Save one leaf-account Cash Flow Setup rule."""
    assignment = _clean_cashflow_assignment(payload)
    _upload, accounts = _latest_control_accounts(assignment["client_id"])
    account = next(
        (row for row in accounts if _normalise_account_code(row["account_code"]) == assignment["account_code"]),
        None,
    )
    if not account:
        raise HTTPException(400, "Account code is not present in the latest Trial Balance")
    if account["classification"] != "leaf":
        raise HTTPException(400, "Assign the parent through the bulk child operation; direct assignments require a leaf account")
    _upsert_cashflow_assignment(assignment, _actor_name(session))
    return {"saved": True, "account_code": assignment["account_code"], "cashflow": _public_cashflow_assignment(assignment)}


@router.post("/api/account-control-center/cashflow/apply-to-children")
def apply_account_control_cashflow_to_children(payload: CashFlowBulkAssignmentPayload, session=Depends(require_admin)):
    """Apply one Cash Flow Setup rule to every latest-Trial-Balance leaf below a parent."""
    parent_code = _clean_control_account_code(payload.parent_account_code)
    assignment = _clean_cashflow_assignment(
        CashFlowAssignmentPayload(
            client_id=payload.client_id,
            account_code=parent_code,
            section=payload.section,
            category_label=payload.category_label,
            cash_bank_role=payload.cash_bank_role,
            treatment_type=payload.treatment_type,
        )
    )
    _upload, accounts = _latest_control_accounts(assignment["client_id"])
    parent = next(
        (row for row in accounts if _normalise_account_code(row["account_code"]) == parent_code),
        None,
    )
    if not parent:
        raise HTTPException(400, "Parent account is not present in the latest Trial Balance")
    leaves = [
        row for row in accounts
        if row["classification"] == "leaf"
        and (
            _normalise_account_code(row["account_code"]) == parent_code
            or _normalise_account_code(row["account_code"]).startswith(parent_code + "-")
        )
    ]
    if not leaves:
        raise HTTPException(400, "No leaf accounts were found below the selected account")
    actor = _actor_name(session)
    with pg_transaction():
        for row in leaves:
            child_assignment = {**assignment, "account_code": row["account_code"]}
            _upsert_cashflow_assignment(child_assignment, actor)
    _refresh_cashflow_snapshot(payload.client_id, "category_bulk_assignment")


    return {"saved": True, "parent_account_code": parent_code, "leaf_count": len(leaves)}

@router.get("/api/account-control-center/cashflow/setup")
def get_cashflow_setup(client_id: int = Query(..., ge=1), session=Depends(require_admin)):
    upload, accounts = _latest_control_accounts(client_id)
    historical_orphans = discover_historical_orphan_accounts_postgres(client_id, accounts)
    categories = execute_query_pg(
        """SELECT id, client_id, category_key, label, sort_order, is_active
           FROM account_control_cashflow_categories
           WHERE client_id = %s
           ORDER BY sort_order, LOWER(label), category_key""",
        (client_id,),
    )
    assignments = execute_query_pg(
        """SELECT account_code, category_id
           FROM account_control_cashflow_category_assignments
           WHERE client_id = %s""",
        (client_id,),
    )
    assignment_by_code = {_normalise_account_code(row["account_code"]): row.get("category_id") for row in assignments}
    historical_orphans = [
        {**dict(orphan), "category_id": assignment_by_code.get(_normalise_account_code(orphan.get("account_code")))}
        for orphan in historical_orphans
    ]
    treasury_rows = execute_query_pg(
        """SELECT account_code, treasury_type
           FROM account_control_cashflow_treasury_accounts
           WHERE client_id = %s AND is_active = TRUE
           ORDER BY treasury_type, account_code""",
        (client_id,),
    )
    settings_rows = execute_query_pg(
        """SELECT match_internal_transfers, receipt_presentation, source_template_id
           FROM account_control_cashflow_settings
           WHERE client_id = %s""",
        (client_id,),
    )
    role_rows = execute_query_pg(
        """SELECT role_key, category_id
           FROM account_control_cashflow_role_categories
           WHERE client_id = %s
           ORDER BY role_key, category_id""",
        (client_id,),
    )
    settings = settings_rows[0] if settings_rows else {"match_internal_transfers": False, "receipt_presentation": "net", "source_template_id": None}
    role_categories = {role: [] for role in _CASHFLOW_ROLE_CATEGORY_KEYS}
    for row in role_rows:
        role = row.get("role_key")
        if role in role_categories:
            role_categories[role].append(int(row["category_id"]))
    return {
        "client_id": client_id,
        "upload": upload,
        "categories": [dict(row) for row in categories],
        "accounts": [
            {
                "id": row["id"],
                "account_code": row["account_code"],
                "account_name": row["account_name"],
                "parent_account_code": row["parent_account_code"],
                "level": row["level"],
                "is_leaf": row["classification"] == "leaf",
                "category_id": assignment_by_code.get(_normalise_account_code(row["account_code"])),
            }
            for row in accounts
        ],
        "historical_orphans": historical_orphans,
        "treasury_accounts": {
            "cash": [row["account_code"] for row in treasury_rows if row["treasury_type"] == "cash"],
            "bank": [row["account_code"] for row in treasury_rows if row["treasury_type"] == "bank"],
        },
        "settings": {
            "match_internal_transfers": bool(settings.get("match_internal_transfers")),
            "receipt_presentation": settings.get("receipt_presentation") or "net",
            "source_template_id": settings.get("source_template_id"),
            "role_categories": role_categories,
        },
    }


# CASHFLOW_CATEGORY_ROUTES
@router.post("/api/account-control-center/cashflow/categories", status_code=201)
def create_cashflow_category(payload: CashFlowCategoryPayload, session=Depends(require_admin)):
    category = _clean_cashflow_category_payload(payload)
    _require_active_client(category["client_id"])
    existing = execute_query_pg(
        "SELECT id FROM account_control_cashflow_categories WHERE client_id = %s AND category_key = %s",
        (category["client_id"], category["category_key"]),
    )
    if existing:
        raise HTTPException(409, "A Cash Flow category with this key already exists")
    execute_write_pg(
        """INSERT INTO account_control_cashflow_categories
           (client_id, category_key, label, sort_order, is_active, updated_at)
           VALUES (%s, %s, %s, %s, %s, CURRENT_TIMESTAMP)""",
        (category["client_id"], category["category_key"], category["label"], category["sort_order"], category["is_active"]),
    )
    _refresh_cashflow_snapshot(category["client_id"], "category_created")


    return {"saved": True, "category": category}

@router.put("/api/account-control-center/cashflow/categories/{category_id}")
def update_cashflow_category(category_id: int, payload: CashFlowCategoryUpdatePayload, session=Depends(require_admin), background_tasks: BackgroundTasks = None):
    category = _clean_cashflow_category_update(payload)
    rows = execute_query_pg(
        "SELECT id FROM account_control_cashflow_categories WHERE id = %s AND client_id = %s",
        (category_id, category["client_id"]),
    )
    if not rows:
        raise HTTPException(404, "Cash Flow category not found")
    execute_write_pg(
        """UPDATE account_control_cashflow_categories
           SET label = %s, sort_order = %s, is_active = %s, updated_at = CURRENT_TIMESTAMP
           WHERE id = %s AND client_id = %s""",
        (category["label"], category["sort_order"], category["is_active"], category_id, category["client_id"]),
    )
    preparation_job = _refresh_cashflow_snapshot(
        category["client_id"],
        "category_updated",
        category_ids=[category_id],
        background_tasks=background_tasks,
    )


    return {"saved": True, "category": {"id": category_id, **category}, "cashflow_preparation_job": preparation_job}

@router.put("/api/account-control-center/cashflow/category-assignment")
def save_cashflow_category_assignment(payload: CashFlowCategoryAssignmentPayload, session=Depends(require_admin), background_tasks: BackgroundTasks = None):
    assignment = _clean_cashflow_category_assignment(payload)
    _validate_cashflow_category(assignment["client_id"], assignment["category_id"])
    _upload, accounts = _latest_control_accounts(assignment["client_id"])
    historical_orphans = discover_historical_orphan_accounts_postgres(assignment["client_id"], accounts)
    account = next((row for row in accounts if _normalise_account_code(row["account_code"]) == assignment["account_code"]), None)
    orphan = next((row for row in historical_orphans if _normalise_account_code(row["account_code"]) == assignment["account_code"]), None)
    if not account and not orphan:
        raise HTTPException(400, "Account code is not present in the latest Trial Balance or historical transaction history")
    if account and account["classification"] != "leaf":
        raise HTTPException(400, "Assign the parent through the bulk child operation; direct assignments require a leaf account")
    _upsert_cashflow_category_assignment(assignment, _actor_name(session))
    preparation_job = _refresh_cashflow_snapshot(
        assignment["client_id"],
        "category_assignment",
        category_account_codes=[assignment["account_code"]],
        background_tasks=background_tasks,
    )
    return {
        "saved": True,
        "account_code": assignment["account_code"],
        "category_id": assignment["category_id"],
        "classification_scope": "historical_orphan" if orphan and not account else "current_trial_balance",
        "cashflow_preparation_job": preparation_job,
    }


@router.post("/api/account-control-center/cashflow/category-assignment/apply-to-children")
def apply_cashflow_category_to_children(payload: CashFlowCategoryBulkAssignmentPayload, session=Depends(require_admin), background_tasks: BackgroundTasks = None):
    parent_code = _clean_control_account_code(payload.parent_account_code)
    _validate_cashflow_category(payload.client_id, payload.category_id)
    _upload, accounts = _latest_control_accounts(payload.client_id)
    parent = next((row for row in accounts if _normalise_account_code(row["account_code"]) == parent_code), None)
    if not parent:
        raise HTTPException(400, "Parent account is not present in the latest Trial Balance")
    leaves = [
        row for row in accounts
        if row["classification"] == "leaf"
        and (_normalise_account_code(row["account_code"]) == parent_code or _normalise_account_code(row["account_code"]).startswith(parent_code + "-"))
    ]
    historical_orphans = discover_historical_orphan_accounts_postgres(payload.client_id, accounts)
    historical_children = [
        row for row in historical_orphans
        if _account_code_is_within(_normalise_account_code(row.get("parent_account_code")), parent_code)
    ]
    if not leaves and not historical_children:
        raise HTTPException(400, "No leaf accounts were found below the selected account")
    actor = _actor_name(session)
    with pg_transaction():
        for row in leaves + historical_children:
            _upsert_cashflow_category_assignment(
                {"client_id": payload.client_id, "account_code": row["account_code"], "category_id": payload.category_id},
                actor,
            )
    preparation_job = _refresh_cashflow_snapshot(
        payload.client_id,
        "category_bulk_assignment",
        category_account_codes=[row["account_code"] for row in leaves + historical_children],
        background_tasks=background_tasks,
    )


    return {"saved": True, "parent_account_code": parent_code, "leaf_count": len(leaves) + len(historical_children), "historical_orphan_count": len(historical_children), "cashflow_preparation_job": preparation_job}

@router.put("/api/account-control-center/cashflow/treasury-accounts")
def save_cashflow_treasury_accounts(payload: CashFlowTreasuryAccountsPayload, session=Depends(require_admin), background_tasks: BackgroundTasks = None):
    cash_codes = {_clean_control_account_code(code) for code in payload.cash_account_codes}
    bank_codes = {_clean_control_account_code(code) for code in payload.bank_account_codes}
    if cash_codes & bank_codes:
        raise HTTPException(400, "An account cannot be selected as both Cash and Bank")
    _upload, accounts = _latest_control_accounts(payload.client_id)
    leaves = {_normalise_account_code(row["account_code"]) for row in accounts if row["classification"] == "leaf"}
    invalid = (cash_codes | bank_codes) - leaves
    if invalid:
        raise HTTPException(400, "Cash and Bank selections must be leaf accounts from the latest Trial Balance")
    actor = _actor_name(session)
    early_change_codes = _cashflow_treasury_change_codes(
        payload.client_id,
        cash_codes,
        bank_codes,
    )
    recovery_required = cashflow_preparation_requires_recovery(
        execute_query_pg,
        client_id=payload.client_id,
    )
    preparation_job = None
    if early_change_codes or recovery_required:
        with pg_transaction() as conn:
            preparation_job = enqueue_cashflow_preparation_job(
                conn,
                client_id=payload.client_id,
                reason="preparation_recovery" if recovery_required else "treasury_accounts",
                affected_account_codes=() if recovery_required else early_change_codes,
            )
    try:
        with pg_transaction() as conn:
            synchronization = synchronize_treasury_accounts_postgres(
                conn,
                client_id=payload.client_id,
                cash_account_codes=sorted(cash_codes),
                bank_account_codes=sorted(bank_codes),
                changed_by=actor,
            )
    except Exception as error:
        if preparation_job:
            try:
                with pg_transaction() as conn:
                    mark_cashflow_preparation_job_failed(
                        conn,
                        job_id=preparation_job["job_id"],
                        error=error,
                    )
            except Exception as cleanup_error:
                logger.warning(
                    "cashflows_preparation_job_failure_record_failed client_id=%s job_id=%s error=%s",
                    payload.client_id,
                    preparation_job["job_id"],
                    cleanup_error,
                )
        raise
    preparation_job = _refresh_cashflow_snapshot(
        payload.client_id,
        "treasury_accounts",
        treasury_account_codes=synchronization.get("affected_account_codes", []),
        preparation_job=preparation_job,
        background_tasks=background_tasks,
    )
    return {
        "saved": True,
        "cash_account_codes": sorted(cash_codes),
        "bank_account_codes": sorted(bank_codes),
        "cashflow_preparation_job": preparation_job,
        **synchronization,
    }


@router.put("/api/account-control-center/cashflow/settings")
def save_cashflow_settings(payload: CashFlowSettingsPayload, session=Depends(require_admin)):
    presentation = _normalise_key(payload.receipt_presentation) or "net"
    if presentation not in {"net", "gross"}:
        raise HTTPException(400, "Customer payment presentation must be net or gross")
    _require_active_client(payload.client_id)
    role_categories = _clean_cashflow_role_categories(payload)
    for category_ids in role_categories.values():
        for category_id in category_ids:
            _validate_cashflow_category(payload.client_id, category_id)
    actor = _actor_name(session)
    with pg_transaction():
        execute_write_pg(
            """INSERT INTO account_control_cashflow_settings
               (client_id, match_internal_transfers, receipt_presentation, updated_by, updated_at)
               VALUES (%s, %s, %s, %s, CURRENT_TIMESTAMP)
               ON CONFLICT (client_id) DO UPDATE SET
                 match_internal_transfers = EXCLUDED.match_internal_transfers,
                 receipt_presentation = EXCLUDED.receipt_presentation,
                 updated_by = EXCLUDED.updated_by,
                 updated_at = CURRENT_TIMESTAMP""",
            (payload.client_id, payload.match_internal_transfers, presentation, actor),
        )
        execute_write_pg(
            "DELETE FROM account_control_cashflow_role_categories WHERE client_id = %s",
            (payload.client_id,),
        )
        for role, category_ids in role_categories.items():
            for category_id in category_ids:
                execute_write_pg(
                    """INSERT INTO account_control_cashflow_role_categories
                       (client_id, role_key, category_id, updated_by, updated_at)
                       VALUES (%s, %s, %s, %s, CURRENT_TIMESTAMP)""",
                    (payload.client_id, role, category_id, actor),
                )
    _refresh_cashflow_snapshot(payload.client_id, "cashflow_settings")
    return {
        "saved": True,
        "match_internal_transfers": payload.match_internal_transfers,
        "receipt_presentation": presentation,
        "role_categories": role_categories,
    }

@router.get("/api/account-control-center/sets")
def list_account_sets(client_id: int = Query(..., ge=1), session=Depends(require_admin)):
    _require_active_client(client_id)
    rows = execute_query_pg(
        """
        SELECT s.id, s.client_id, s.set_key, s.active_version_id, s.is_active, s.created_at, s.updated_at,
               v.display_name, v.status AS active_version_status, v.version_number
        FROM account_sets s
        LEFT JOIN account_set_versions v ON v.id = s.active_version_id
        WHERE s.client_id = %s AND s.is_active = TRUE
        ORDER BY LOWER(COALESCE(v.display_name, s.set_key)), s.id
        """,
        (client_id,),
    )
    return {
        "client_id": client_id,
        "account_sets": [
            {
                "id": row["id"],
                "set_key": row["set_key"],
                "active_version_id": row["active_version_id"],
                "display_name": row["display_name"],
                "active_version_status": row["active_version_status"],
                "version_number": row["version_number"],
                "updated_at": row["updated_at"],
            }
            for row in rows
        ],
    }


@router.get("/api/account-control-center/sets/{account_set_id}")
def get_account_set(account_set_id: int, session=Depends(require_admin)):
    return _serialise_set(account_set_id)


@router.post("/api/account-control-center/sets", status_code=201)
def create_account_set(payload: AccountSetCreatePayload, session=Depends(require_admin)):
    _require_active_client(payload.client_id)
    set_key, display_name, description = _clean_set_identity(payload.set_key, payload.display_name, payload.description)
    selectors = _clean_selectors(payload.selectors)
    upload = find_latest_parsed_upload(payload.client_id)
    if not upload:
        raise HTTPException(409, "A parsed Trial Balance is required before creating an Account Set")
    _validate_selectors_against_upload(payload.client_id, upload["id"], selectors)
    existing = execute_query_pg(
        "SELECT id FROM account_sets WHERE client_id = %s AND set_key = %s",
        (payload.client_id, set_key),
    )
    if existing:
        raise HTTPException(409, "An Account Set with this key already exists for the client")
    with pg_transaction():
        account_set_id = execute_write_pg(
            """
            INSERT INTO account_sets (client_id, set_key)
            VALUES (%s, %s) RETURNING id
            """,
            (payload.client_id, set_key),
        )
        version_id = execute_write_pg(
            """
            INSERT INTO account_set_versions
                (account_set_id, version_number, source_upload_id, display_name, description, status)
            VALUES (%s, 1, %s, %s, %s, 'draft') RETURNING id
            """,
            (account_set_id, upload["id"], display_name, description),
        )
        _replace_selectors(version_id, selectors)
    log_audit(_actor_name(session), "Create", "AccountControlCentre", f"Created draft Account Set {set_key}")
    return _serialise_set(account_set_id)


@router.put("/api/account-control-center/versions/{version_id}")
def update_account_set_draft(version_id: int, payload: AccountSetDraftUpdatePayload, session=Depends(require_admin)):
    version = _get_version(version_id)
    if version["status"] != "draft":
        raise HTTPException(409, "Published Account Set versions are immutable; create a new draft instead")
    display_name, description, selectors = _clean_draft_payload(payload)
    upload_id = payload.source_upload_id or version["source_upload_id"]
    _validate_selectors_against_upload(version["client_id"], upload_id, selectors)
    with pg_transaction():
        execute_write_pg(
            """
            UPDATE account_set_versions
            SET source_upload_id = %s, display_name = %s, description = %s
            WHERE id = %s AND status = 'draft'
            """,
            (upload_id, display_name, description, version_id),
        )
        _replace_selectors(version_id, selectors)
        execute_write_pg(
            "UPDATE account_sets SET updated_at = CURRENT_TIMESTAMP WHERE id = %s",
            (version["account_set_id"],),
        )
    log_audit(_actor_name(session), "Update", "AccountControlCentre", f"Updated draft Account Set version {version_id}")
    return _serialise_set(version["account_set_id"])


@router.get("/api/account-control-center/versions/{version_id}/preview")
def preview_account_set_version(
    version_id: int,
    upload_id: Optional[int] = Query(default=None, ge=1),
    session=Depends(require_admin),
):
    version = _get_version(version_id)
    target_upload_id = upload_id or version["source_upload_id"]
    upload, resolved = _validate_selectors_against_upload(
        version["client_id"],
        target_upload_id,
        _version_selectors(version_id),
    )
    return {
        "account_set_id": version["account_set_id"],
        "version_id": version_id,
        "version_status": version["status"],
        "upload": upload,
        "resolved_leaf_accounts": resolved,
        "resolved_leaf_count": len(resolved),
    }


@router.post("/api/account-control-center/versions/{version_id}/publish")
def publish_account_set_version(version_id: int, session=Depends(require_admin)):
    version = _get_version(version_id)
    if version["status"] != "draft":
        raise HTTPException(409, "Only a draft Account Set version can be published")
    _validate_selectors_against_upload(
        version["client_id"],
        version["source_upload_id"],
        _version_selectors(version_id),
        require_resolved_accounts=True,
    )
    with pg_transaction():
        execute_write_pg(
            """
            UPDATE account_set_versions
            SET status = 'superseded'
            WHERE account_set_id = %s AND status = 'published'
            """,
            (version["account_set_id"],),
        )
        execute_write_pg(
            """
            UPDATE account_set_versions
            SET status = 'published', published_at = CURRENT_TIMESTAMP
            WHERE id = %s AND status = 'draft'
            """,
            (version_id,),
        )
        execute_write_pg(
            """
            UPDATE account_sets
            SET active_version_id = %s, updated_at = CURRENT_TIMESTAMP
            WHERE id = %s
            """,
            (version_id, version["account_set_id"]),
        )
    log_audit(_actor_name(session), "Publish", "AccountControlCentre", f"Published Account Set version {version_id}")
    return _serialise_set(version["account_set_id"])


@router.post("/api/account-control-center/sets/{account_set_id}/draft", status_code=201)
def create_account_set_draft(account_set_id: int, session=Depends(require_admin)):
    account_set = _get_set(account_set_id)
    if not account_set["active_version_id"]:
        raise HTTPException(409, "Publish the first Account Set version before creating a revision draft")
    existing_draft = execute_query_pg(
        "SELECT id FROM account_set_versions WHERE account_set_id = %s AND status = 'draft'",
        (account_set_id,),
    )
    if existing_draft:
        raise HTTPException(409, "This Account Set already has a draft version")
    active_version = _get_version(account_set["active_version_id"])
    upload = find_latest_parsed_upload(account_set["client_id"])
    if not upload:
        raise HTTPException(409, "A parsed Trial Balance is required before creating a revision draft")
    next_version = execute_query_pg(
        "SELECT COALESCE(MAX(version_number), 0) + 1 AS next_version FROM account_set_versions WHERE account_set_id = %s",
        (account_set_id,),
    )[0]["next_version"]
    selectors = _version_selectors(active_version["id"])
    with pg_transaction():
        draft_id = execute_write_pg(
            """
            INSERT INTO account_set_versions
                (account_set_id, version_number, source_upload_id, display_name, description, status)
            VALUES (%s, %s, %s, %s, %s, 'draft') RETURNING id
            """,
            (
                account_set_id,
                next_version,
                upload["id"],
                active_version["display_name"],
                active_version["description"],
            ),
        )
        _replace_selectors(draft_id, selectors)
        execute_write_pg("UPDATE account_sets SET updated_at = CURRENT_TIMESTAMP WHERE id = %s", (account_set_id,))
    log_audit(_actor_name(session), "Create", "AccountControlCentre", f"Created Account Set draft {draft_id}")
    return _serialise_set(account_set_id)
