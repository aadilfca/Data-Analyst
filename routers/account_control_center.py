"""Client Account Set foundation for the Chart of Accounts Control Centre."""

from __future__ import annotations

import re
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from database import execute_query_pg, execute_write_pg, log_audit, pg_transaction
from routers.auth import require_admin
from routers.trial_balance import find_latest_parsed_upload


router = APIRouter()

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


def _clean_control_account_code(value: Any) -> str:
    code = _normalise_account_code(value)
    if not code or len(code) > 120:
        raise HTTPException(400, "Account code is required and must be 120 characters or fewer")
    return code


def _clean_control_assignment(payload: AccountControlAssignmentPayload) -> dict[str, Any]:
    account_code = _clean_control_account_code(payload.account_code)
    workstream = _normalise_text(payload.verification_workstream).casefold() or None
    if workstream and workstream not in _VERIFICATION_WORKSTREAMS:
        raise HTTPException(400, "Verification workstream is invalid")
    return {
        "client_id": payload.client_id,
        "account_code": account_code,
        "verification_workstream": workstream,
        "financial_statement_category_id": payload.financial_statement_category_id,
        "note_category_id": payload.note_category_id,
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
            """SELECT id FROM account_control_categories
               WHERE id = %s AND client_id = %s AND category_type = %s AND is_active = TRUE""",
            (category_id, client_id, category_type),
        )
        if not rows:
            raise HTTPException(400, f"Invalid active {category_type.replace('_', ' ')} category")


def _upsert_control_assignment(assignment: dict[str, Any], updated_by: str) -> None:
    execute_write_pg(
        """
        INSERT INTO account_control_assignments
            (client_id, account_code, verification_workstream,
             financial_statement_category_id, note_category_id, updated_by, updated_at)
        VALUES (%s, %s, %s, %s, %s, %s, CURRENT_TIMESTAMP)
        ON CONFLICT (client_id, account_code) DO UPDATE SET
            verification_workstream = EXCLUDED.verification_workstream,
            financial_statement_category_id = EXCLUDED.financial_statement_category_id,
            note_category_id = EXCLUDED.note_category_id,
            updated_by = EXCLUDED.updated_by,
            updated_at = CURRENT_TIMESTAMP
        """,
        (
            assignment["client_id"], assignment["account_code"], assignment["verification_workstream"],
            assignment["financial_statement_category_id"], assignment["note_category_id"], updated_by,
        ),
    )


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
    return {"saved": True, "account_code": assignment["account_code"], "assignment": assignment}


@router.post("/api/account-control-center/mapping/apply-to-children")
def apply_account_control_mapping_to_children(payload: AccountControlBulkAssignmentPayload, session=Depends(require_admin)):
    """Apply one assignment to every latest-Trial-Balance leaf below a selected parent."""
    parent_code = _clean_control_account_code(payload.parent_account_code)
    assignment = _clean_control_assignment(
        AccountControlAssignmentPayload(
            client_id=payload.client_id,
            account_code=parent_code,
            verification_workstream=payload.verification_workstream,
            financial_statement_category_id=payload.financial_statement_category_id,
            note_category_id=payload.note_category_id,
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
    _validate_control_categories(assignment["client_id"], assignment)
    actor = _actor_name(session)
    with pg_transaction():
        for row in leaves:
            child_assignment = {**assignment, "account_code": row["account_code"]}
            _upsert_control_assignment(child_assignment, actor)
    return {"saved": True, "parent_account_code": parent_code, "leaf_count": len(leaves)}


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
