"""Cash/Bank transaction-line classification and controlled backfill.

This module reads existing Client Data rows and Account Control Centre treasury
mappings, then writes only to the derived classification layer. It never
updates client_data.row_data or the uploaded source files.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
import json
import re
from typing import Any, Callable, Mapping

from psycopg2.extras import RealDictCursor, execute_values
from database import execute_query_pg


_FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "voucher_id": (
        "voucher id", "voucher_id", "voucher no", "voucher number", "voucher",
        "transaction id", "transaction ref", "reference no",
    ),
    "account_code": (
        "account detail", "account code", "account_code", "account no",
        "account number", "code", "ledger code", "account head", "ledger detail",
    ),
    "transaction_date": (
        "date", "transaction date", "voucher date", "posting date",
    ),
    "debit": ("debit amount", "debit", "dr", "amount debit"),
    "credit": ("credit amount", "credit", "cr", "amount credit"),
}


def _normalise_label(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").strip().casefold())


def _text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _canonical_code(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return str(value).strip()
    if isinstance(value, (int, float)):
        numeric = float(value)
        if numeric.is_integer():
            return str(int(numeric))
    return re.sub(r"s+", " ", _text(value))


def _decode_row_data(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except (TypeError, ValueError):
            return {}
        return dict(decoded) if isinstance(decoded, Mapping) else {}
    return {}


def _field_value(row_data: Mapping[str, Any], role: str) -> Any:
    values = {
        _normalise_label(key): value
        for key, value in row_data.items()
    }
    for alias in _FIELD_ALIASES[role]:
        normalised = _normalise_label(alias)
        if normalised in values:
            return values[normalised]
    return None


def _parse_amount(value: Any) -> float:
    if value is None or isinstance(value, bool):
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    text = _text(value)
    if not text or text.casefold() in {"-", "n/a", "na"}:
        return 0.0
    negative = text.startswith("(") and text.endswith(")")
    if negative:
        text = text[1:-1]
    if text.endswith("-"):
        negative = True
        text = text[:-1]
    text = re.sub(r"[^0-9.\-]", "", text.replace(",", ""))
    if not text or text in {".", "-", "-."}:
        return 0.0
    try:
        amount = float(text)
    except ValueError:
        return 0.0
    return -amount if negative else amount


def _parse_date(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        numeric = float(value)
        if 20000 <= numeric <= 60000:
            return (datetime(1899, 12, 30) + timedelta(days=numeric)).date().isoformat()
    text = _text(value)
    if not text:
        return None
    if re.fullmatch(r"\d+(?:\.0+)?", text):
        numeric = float(text)
        if 20000 <= numeric <= 60000:
            return (datetime(1899, 12, 30) + timedelta(days=numeric)).date().isoformat()
    for fmt in (
        "%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d", "%Y%m%d",
        "%d-%m-%Y", "%d/%m/%Y", "%d.%m.%Y",
        "%d-%m-%y", "%d/%m/%y", "%d.%m.%y",
        "%d-%b-%Y", "%d/%b/%Y", "%d-%b-%y", "%d/%b/%y",
    ):
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        return None


def build_treasury_mapping(rows: list[Mapping[str, Any]]) -> dict[str, set[str]]:
    """Return normalized account-code -> one or more treasury types."""
    mapping: dict[str, set[str]] = {}
    for row in rows:
        account_code = _canonical_code(row.get("account_code"))
        treasury_type = _text(row.get("treasury_type")).casefold()
        if account_code and treasury_type in {"cash", "bank"}:
            mapping.setdefault(account_code, set()).add(treasury_type)
    return mapping


def build_classification_row(
    source_row: Mapping[str, Any],
    treasury_mapping: Mapping[str, set[str]],
    configuration_version: int,
) -> dict[str, Any]:
    """Build one derived row without modifying the source row."""
    row_data = _decode_row_data(source_row.get("row_data"))
    account_code = _canonical_code(_field_value(row_data, "account_code"))
    voucher_id = _text(_field_value(row_data, "voucher_id"))
    transaction_date = _parse_date(_field_value(row_data, "transaction_date"))
    if transaction_date is None:
        transaction_date = _parse_date(source_row.get("extract_date"))

    treasury_types = treasury_mapping.get(account_code, set())
    if len(treasury_types) == 1:
        treasury_type = next(iter(treasury_types))
        classification_status = treasury_type
        treasury_account_code = account_code
    elif not account_code or len(treasury_types) > 1:
        treasury_type = None
        classification_status = "unmapped"
        treasury_account_code = None
    else:
        treasury_type = None
        classification_status = "non_treasury"
        treasury_account_code = None

    return {
        "client_data_id": int(source_row["id"]),
        "client_id": int(source_row["client_id"]),
        "template_id": int(source_row["template_id"]),
        "upload_id": int(source_row["upload_id"]),
        "voucher_id": voucher_id or None,
        "account_code": account_code or None,
        "transaction_date": transaction_date,
        "debit": _parse_amount(_field_value(row_data, "debit")),
        "credit": _parse_amount(_field_value(row_data, "credit")),
        "treasury_type": treasury_type,
        "treasury_account_code": treasury_account_code,
        "configuration_version": int(configuration_version),
        "classification_status": classification_status,
        "is_current": 1,
    }


def load_treasury_mapping_postgres(conn: Any, client_id: int) -> dict[str, set[str]]:
    """Load the current Account Control Centre treasury mapping on one connection."""
    with conn.cursor(cursor_factory=RealDictCursor) as cursor:
        cursor.execute(
            """
            SELECT account_code, treasury_type
            FROM account_control_cashflow_treasury_accounts
            WHERE client_id = %s AND is_active = TRUE
            """,
            (client_id,),
        )
        return build_treasury_mapping([dict(row) for row in cursor.fetchall()])


def _configuration_action(current: set[tuple[str, str]], requested: set[tuple[str, str]]) -> str:
    """Name the audit event without exposing implementation details to the router."""
    if not current:
        return "created"
    if current - requested and not requested - current:
        return "deactivated"
    if requested - current and not current - requested:
        return "added"
    return "edited"


def synchronize_treasury_accounts_postgres(
    conn: Any,
    *,
    client_id: int,
    cash_account_codes: list[str],
    bank_account_codes: list[str],
    changed_by: str,
) -> dict[str, Any]:
    """Save a versioned Cash/Bank snapshot and reclassify existing lines atomically.

    The caller owns the surrounding transaction. The current mapping is soft
    deactivated, the requested mapping becomes active, and a new derived
    classification version is written without changing client_data.row_data.
    """
    requested_pairs = {
        (str(code), "cash") for code in cash_account_codes
    } | {
        (str(code), "bank") for code in bank_account_codes
    }
    with conn.cursor(cursor_factory=RealDictCursor) as cursor:
        cursor.execute(
            """
            SELECT account_code, treasury_type
            FROM account_control_cashflow_treasury_accounts
            WHERE client_id = %s AND is_active = TRUE
            """,
            (client_id,),
        )
        current_pairs = {
            (str(row["account_code"]), str(row["treasury_type"]))
            for row in cursor.fetchall()
        }
        cursor.execute(
            """
            SELECT COALESCE(MAX(version_number), 0) AS current_version
            FROM account_control_cashflow_config_versions
            WHERE client_id = %s
            """,
            (client_id,),
        )
        current_version = int(cursor.fetchone()["current_version"] or 0)

    if current_pairs == requested_pairs:
        return {
            "configuration_version": current_version or 1,
            "reclassified_rows": 0,
            "changed": False,
            "action": "unchanged",
        }

    action = _configuration_action(current_pairs, requested_pairs)
    configuration_version = current_version + 1
    with conn.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO account_control_cashflow_config_versions
                (client_id, version_number, change_action, changed_by)
            VALUES (%s, %s, %s, %s)
            RETURNING id
            """,
            (client_id, configuration_version, action, changed_by),
        )
        config_version_id = cursor.fetchone()[0]
        if requested_pairs:
            execute_values(
                cursor,
                """
                INSERT INTO account_control_cashflow_config_entries
                    (configuration_version_id, client_id, account_code, treasury_type, is_active, changed_by)
                VALUES %s
                """,
                [
                    (config_version_id, client_id, account_code, treasury_type, True, changed_by)
                    for account_code, treasury_type in sorted(requested_pairs)
                ],
                template="(%s, %s, %s, %s, %s, %s)",
                page_size=500,
            )
        cursor.execute(
            """
            UPDATE account_control_cashflow_treasury_accounts
            SET is_active = FALSE, updated_by = %s, updated_at = CURRENT_TIMESTAMP
            WHERE client_id = %s
            """,
            (changed_by, client_id),
        )
        if requested_pairs:
            execute_values(
                cursor,
                """
                INSERT INTO account_control_cashflow_treasury_accounts
                    (client_id, account_code, treasury_type, is_active, updated_by, updated_at)
                VALUES %s
                ON CONFLICT (client_id, treasury_type, account_code) DO UPDATE SET
                    is_active = TRUE,
                    updated_by = EXCLUDED.updated_by,
                    updated_at = CURRENT_TIMESTAMP
                """,
                [
                    (client_id, account_code, treasury_type, True, changed_by)
                    for account_code, treasury_type in sorted(requested_pairs)
                ],
                template="(%s, %s, %s, %s, %s, CURRENT_TIMESTAMP)",
                page_size=500,
            )
        cursor.execute(
            """
            UPDATE cash_bank_transaction_classifications
            SET is_current = FALSE
            WHERE client_id = %s AND is_current = TRUE
            """,
            (client_id,),
        )
        cursor.execute(
            """
            SELECT id, client_id, template_id, upload_id, row_data, extract_date
            FROM client_data
            WHERE client_id = %s AND is_deleted = FALSE
            ORDER BY id
            """,
            (client_id,),
        )
        source_rows = [dict(row) for row in cursor.fetchall()]

    mapping = build_treasury_mapping(
        [
            {"account_code": account_code, "treasury_type": treasury_type}
            for account_code, treasury_type in requested_pairs
        ]
    )
    upsert_classification_rows_postgres(
        conn, source_rows, mapping, configuration_version=configuration_version
    )
    return {
        "configuration_version": configuration_version,
        "reclassified_rows": len(source_rows),
        "changed": True,
        "action": action,
    }
def upsert_classification_rows_postgres(
    conn: Any,
    source_rows: list[Mapping[str, Any]],
    treasury_mapping: Mapping[str, set[str]],
    configuration_version: int = 1,
) -> list[dict[str, Any]]:
    """Upsert derived rows for new or changed upload lines without committing."""
    derived_rows = [
        build_classification_row(row, treasury_mapping, configuration_version)
        for row in source_rows
    ]
    if not derived_rows:
        return []

    values = [
        (
            row["client_data_id"],
            row["client_id"],
            row["template_id"],
            row["upload_id"],
            row["voucher_id"],
            row["account_code"],
            row["transaction_date"],
            row["debit"],
            row["credit"],
            row["treasury_type"],
            row["treasury_account_code"],
            row["configuration_version"],
            row["classification_status"],
            True,
        )
        for row in derived_rows
    ]
    with conn.cursor() as cursor:
        execute_values(
            cursor,
            """
            INSERT INTO cash_bank_transaction_classifications (
                client_data_id, client_id, template_id, upload_id,
                voucher_id, account_code, transaction_date, debit, credit,
                treasury_type, treasury_account_code, configuration_version,
                classification_status, is_current
            ) VALUES %s
            ON CONFLICT (client_data_id, configuration_version)
            DO UPDATE SET
                client_id = EXCLUDED.client_id,
                template_id = EXCLUDED.template_id,
                upload_id = EXCLUDED.upload_id,
                voucher_id = EXCLUDED.voucher_id,
                account_code = EXCLUDED.account_code,
                transaction_date = EXCLUDED.transaction_date,
                debit = EXCLUDED.debit,
                credit = EXCLUDED.credit,
                treasury_type = EXCLUDED.treasury_type,
                treasury_account_code = EXCLUDED.treasury_account_code,
                classification_status = EXCLUDED.classification_status,
                is_current = EXCLUDED.is_current,
                classified_at = CURRENT_TIMESTAMP
            """,
            values,
            page_size=500,
        )
    return derived_rows

@dataclass
class BackfillSummary:
    backend: str
    configuration_version: int
    dry_run: bool
    client_ids: list[int]
    source_rows: int = 0
    inserted_rows: int = 0
    skipped_existing_rows: int = 0
    cash_rows: int = 0
    bank_rows: int = 0
    non_treasury_rows: int = 0
    unmapped_rows: int = 0

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def load_cashflow_classification_preview(
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
    query_fn: Callable[..., list[dict[str, Any]]] = execute_query_pg,
) -> dict[str, Any]:
    """Return one read-only, paginated Client Data/classification view.

    Raw ``client_data.row_data`` is selected but never updated. The three
    Cashflows fields are resolved in SQL from the current derived row and the
    active Account Control Centre category assignment.
    """
    if page < 1 or limit < 1 or limit > 200:
        raise ValueError("Invalid classification preview pagination")

    where = [
        "cd.client_id = %s",
        "cd.template_id = %s",
        "cd.is_deleted = FALSE",
        "c.is_current = TRUE",
        "c.transaction_date >= %s",
        "c.transaction_date <= %s",
    ]
    params: list[Any] = [client_id, template_id, period_start, period_end]

    if search:
        like = f"%{search.strip()}%"
        where.append(
            "("
            "LOWER(COALESCE(c.voucher_id, '')) LIKE LOWER(%s) "
            "OR LOWER(COALESCE(c.account_code, '')) LIKE LOWER(%s) "
            "OR LOWER(COALESCE(category.label, '')) LIKE LOWER(%s) "
            "OR EXISTS (SELECT 1 FROM jsonb_each_text(cd.row_data) pair "
            "WHERE LOWER(pair.value) LIKE LOWER(%s))"
            ")"
        )
        params.extend([like, like, like, like])

    if classification_type == "cash":
        where.append("c.treasury_type = 'cash'")
    elif classification_type == "bank":
        where.append("c.treasury_type = 'bank'")
    elif classification_type == "counter":
        where.append("c.classification_status = 'non_treasury'")
    elif classification_type == "review":
        where.append("c.classification_status = 'unmapped'")
    elif classification_type not in (None, ""):
        raise ValueError("Invalid classification type")

    if account_code:
        where.append("LOWER(c.account_code) = LOWER(%s)")
        params.append(account_code.strip())
    if category:
        category_filter = category.strip().casefold()
        if category_filter == "__unassigned__":
            where.append("NULLIF(BTRIM(COALESCE(category.label, '')), '') IS NULL")
        else:
            where.append("LOWER(COALESCE(category.label, '')) LIKE LOWER(%s)")
            params.append(f"%{category.strip()}%")

    where_sql = " AND ".join(where)
    from_sql = """
        FROM client_data cd
        JOIN cash_bank_transaction_classifications c
          ON c.client_data_id = cd.id
        LEFT JOIN account_control_cashflow_category_assignments category_assignment
          ON category_assignment.client_id = c.client_id
         AND category_assignment.account_code = c.account_code
        LEFT JOIN account_control_cashflow_categories category
          ON category.id = category_assignment.category_id
         AND category.client_id = c.client_id
         AND category.is_active = TRUE
    """
    count_rows = query_fn(
        f"SELECT COUNT(*) AS total {from_sql} WHERE {where_sql}",
        tuple(params),
    )
    total = int(count_rows[0].get("total") or 0) if count_rows else 0

    offset = (page - 1) * limit
    data_sql = f"""
        SELECT
            cd.id,
            cd.row_data,
            cd.extract_amount,
            cd.extract_date,
            cd.data_quality_flag,
            cd.rate_source,
            c.voucher_id,
            c.account_code,
            c.transaction_date,
            c.debit,
            c.credit,
            c.treasury_type,
            c.treasury_account_code,
            c.configuration_version,
            c.classification_status,
            COALESCE(category.label, '') AS assigned_category,
            COALESCE(
                cd.row_data ->> 'Account Name',
                cd.row_data ->> 'Account Title',
                cd.row_data ->> 'Ledger Name',
                cd.row_data ->> 'Particulars',
                ''
            ) AS source_account_name
        {from_sql}
        WHERE {where_sql}
        ORDER BY c.transaction_date DESC NULLS LAST, c.voucher_id, c.account_code, cd.id
        LIMIT %s OFFSET %s
    """
    rows = query_fn(data_sql, tuple([*params, limit, offset]))
    formatted: list[dict[str, Any]] = []
    for row in rows:
        status = row.get("classification_status") or "unmapped"
        treasury_type = row.get("treasury_type")
        if treasury_type in ("cash", "bank"):
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
                "account_code": row.get("treasury_account_code") or row.get("account_code"),
                "account_name": row.get("source_account_name") or "",
                "assigned_category": row.get("assigned_category") or "",
                "classification_status": status,
                "voucher_id": row.get("voucher_id"),
                "transaction_date": row.get("transaction_date"),
                "debit": row.get("debit") or 0,
                "credit": row.get("credit") or 0,
                "configuration_version": row.get("configuration_version"),
            },
        })
    return {"total_rows": total, "page": page, "limit": limit, "rows": formatted}

def _record_classification_counts(summary: BackfillSummary, row: Mapping[str, Any]) -> None:
    status = row["classification_status"]
    if status == "cash":
        summary.cash_rows += 1
    elif status == "bank":
        summary.bank_rows += 1
    elif status == "non_treasury":
        summary.non_treasury_rows += 1
    else:
        summary.unmapped_rows += 1


def _client_ids_sqlite(conn: Any, client_id: int | None) -> list[int]:
    if client_id is not None:
        return [int(client_id)]
    rows = conn.execute(
        "SELECT DISTINCT client_id FROM client_data WHERE is_deleted = 0 ORDER BY client_id"
    ).fetchall()
    return [int(row[0]) for row in rows]


def backfill_sqlite(
    conn: Any,
    *,
    client_id: int | None = None,
    configuration_version: int = 1,
    batch_size: int = 500,
    dry_run: bool = False,
) -> BackfillSummary:
    """Backfill SQLite derived classifications in committed batches."""
    if batch_size < 1:
        raise ValueError("batch_size must be positive")
    client_ids = _client_ids_sqlite(conn, client_id)
    summary = BackfillSummary("sqlite", int(configuration_version), dry_run, client_ids)

    insert_sql = """
        INSERT OR IGNORE INTO cash_bank_transaction_classifications (
            client_data_id, client_id, template_id, upload_id,
            voucher_id, account_code, transaction_date, debit, credit,
            treasury_type, treasury_account_code, configuration_version,
            classification_status, is_current
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """

    for current_client_id in client_ids:
        mapping_rows = conn.execute(
            """
            SELECT account_code, treasury_type
            FROM account_control_cashflow_treasury_accounts
            WHERE client_id = ?
            """,
            (current_client_id,),
        ).fetchall()
        mapping = build_treasury_mapping([dict(row) for row in mapping_rows])
        source_cursor = conn.execute(
            """
            SELECT id, client_id, template_id, upload_id, row_data, extract_date
            FROM client_data
            WHERE client_id = ? AND is_deleted = 0
            ORDER BY id
            """,
            (current_client_id,),
        )
        while True:
            source_rows = source_cursor.fetchmany(batch_size)
            if not source_rows:
                break
            source_dicts = [dict(row) for row in source_rows]
            ids = [int(row["id"]) for row in source_dicts]
            existing_rows = conn.execute(
                f"""
                SELECT client_data_id
                FROM cash_bank_transaction_classifications
                WHERE configuration_version = ?
                  AND client_data_id IN ({",".join("?" for _ in ids)})
                """,
                (configuration_version, *ids),
            ).fetchall()
            existing_ids = {int(row[0]) for row in existing_rows}
            insert_rows = []
            for source_row in source_dicts:
                summary.source_rows += 1
                derived_row = build_classification_row(
                    source_row, mapping, configuration_version
                )
                _record_classification_counts(summary, derived_row)
                if derived_row["client_data_id"] in existing_ids:
                    summary.skipped_existing_rows += 1
                else:
                    insert_rows.append(derived_row)

            if not dry_run and insert_rows:
                try:
                    conn.executemany(
                        insert_sql,
                        [
                            (
                                row["client_data_id"],
                                row["client_id"],
                                row["template_id"],
                                row["upload_id"],
                                row["voucher_id"],
                                row["account_code"],
                                row["transaction_date"],
                                row["debit"],
                                row["credit"],
                                row["treasury_type"],
                                row["treasury_account_code"],
                                row["configuration_version"],
                                row["classification_status"],
                                row["is_current"],
                            )
                            for row in insert_rows
                        ],
                    )
                    conn.commit()
                except Exception:
                    conn.rollback()
                    raise
                summary.inserted_rows += len(insert_rows)

    return summary


def _client_ids_postgres(conn: Any, client_id: int | None) -> list[int]:
    with conn.cursor() as cursor:
        if client_id is None:
            cursor.execute(
                "SELECT DISTINCT client_id FROM client_data WHERE is_deleted = FALSE ORDER BY client_id"
            )
            return [int(row[0]) for row in cursor.fetchall()]
    return [int(client_id)]


def backfill_postgres(
    conn: Any,
    *,
    client_id: int | None = None,
    configuration_version: int = 1,
    batch_size: int = 500,
    dry_run: bool = False,
) -> BackfillSummary:
    """Backfill PostgreSQL derived classifications in committed batches."""
    if batch_size < 1:
        raise ValueError("batch_size must be positive")
    client_ids = _client_ids_postgres(conn, client_id)
    summary = BackfillSummary("postgres", int(configuration_version), dry_run, client_ids)
    insert_sql = """
        INSERT INTO cash_bank_transaction_classifications (
            client_data_id, client_id, template_id, upload_id,
            voucher_id, account_code, transaction_date, debit, credit,
            treasury_type, treasury_account_code, configuration_version,
            classification_status, is_current
        ) VALUES %s
        ON CONFLICT (client_data_id, configuration_version) DO NOTHING
    """

    try:
        for current_client_id in client_ids:
            # Keep the source cursor alive while checking each batch against
            # the derived table. Reusing one cursor for both queries resets
            # the source result set after the first batch.
            with (
                conn.cursor(cursor_factory=RealDictCursor) as source_cursor,
                conn.cursor(cursor_factory=RealDictCursor) as lookup_cursor,
            ):
                lookup_cursor.execute(
                    """
                    SELECT account_code, treasury_type
                    FROM account_control_cashflow_treasury_accounts
                    WHERE client_id = %s AND is_active = TRUE
                    """,
                    (current_client_id,),
                )
                mapping = build_treasury_mapping([dict(row) for row in lookup_cursor.fetchall()])
                source_cursor.execute(
                    """
                    SELECT id, client_id, template_id, upload_id, row_data, extract_date
                    FROM client_data
                    WHERE client_id = %s AND is_deleted = FALSE
                    ORDER BY id
                    """,
                    (current_client_id,),
                )
                while True:
                    source_rows = source_cursor.fetchmany(batch_size)
                    if not source_rows:
                        break
                    source_dicts = [dict(row) for row in source_rows]
                    ids = [int(row["id"]) for row in source_dicts]
                    lookup_cursor.execute(
                        """
                        SELECT client_data_id
                        FROM cash_bank_transaction_classifications
                        WHERE configuration_version = %s
                          AND client_data_id = ANY(%s)
                        """,
                        (configuration_version, ids),
                    )
                    existing_ids = {int(row["client_data_id"]) for row in lookup_cursor.fetchall()}
                    insert_rows = []
                    for source_row in source_dicts:
                        summary.source_rows += 1
                        derived_row = build_classification_row(
                            source_row, mapping, configuration_version
                        )
                        _record_classification_counts(summary, derived_row)
                        if derived_row["client_data_id"] in existing_ids:
                            summary.skipped_existing_rows += 1
                        else:
                            insert_rows.append(derived_row)

                    if not dry_run and insert_rows:
                        execute_values(
                            lookup_cursor,
                            insert_sql,
                            [
                                (
                                    row["client_data_id"],
                                    row["client_id"],
                                    row["template_id"],
                                    row["upload_id"],
                                    row["voucher_id"],
                                    row["account_code"],
                                    row["transaction_date"],
                                    row["debit"],
                                    row["credit"],
                                    row["treasury_type"],
                                    row["treasury_account_code"],
                                    row["configuration_version"],
                                    row["classification_status"],
                                    True,
                                )
                                for row in insert_rows
                            ],
                            page_size=batch_size,
                        )
                        conn.commit()
                        summary.inserted_rows += len(insert_rows)
    except Exception:
        conn.rollback()
        raise

    return summary


def discover_historical_orphan_accounts_postgres(
    client_id: int,
    current_accounts: list[Mapping[str, Any]],
    query_fn: Callable[..., list[Mapping[str, Any]]] | None = None,
) -> list[dict[str, Any]]:
    """Return historical transaction account codes absent from the latest TB."""
    query = query_fn or execute_query_pg
    rows = query(
        """
        SELECT
            c.account_code,
            COUNT(*) AS transaction_count,
            COUNT(DISTINCT c.voucher_id) AS voucher_count,
            MIN(c.transaction_date) AS first_transaction_date,
            MAX(c.transaction_date) AS last_transaction_date,
            COALESCE(
                MAX(NULLIF(BTRIM(cd.row_data ->> 'Account Name'), '')),
                MAX(NULLIF(BTRIM(cd.row_data ->> 'Account Title'), '')),
                MAX(NULLIF(BTRIM(cd.row_data ->> 'Ledger Name'), '')),
                MAX(NULLIF(BTRIM(cd.row_data ->> 'Particulars'), '')),
                ''
            ) AS source_account_name,
            MAX(orphan_category_assignment.category_id) AS category_id
        FROM cash_bank_transaction_classifications c
        JOIN client_data cd ON cd.id = c.client_data_id
        LEFT JOIN account_control_cashflow_category_assignments orphan_category_assignment
          ON orphan_category_assignment.client_id = c.client_id
         AND orphan_category_assignment.account_code = c.account_code
        WHERE c.client_id = %s
          AND NULLIF(BTRIM(c.account_code), '') IS NOT NULL
        GROUP BY c.account_code
        ORDER BY c.account_code
        """,
        (client_id,),
    )
    current_by_code = {
        str(account.get("account_code") or "").strip().casefold(): account
        for account in current_accounts
        if str(account.get("account_code") or "").strip()
    }

    def nearest_parent(code: str) -> dict[str, Any] | None:
        parts = code.split("-")
        for index in range(len(parts) - 1, 0, -1):
            parent = current_by_code.get("-".join(parts[:index]).casefold())
            if parent:
                return parent
        return None

    discovered: list[dict[str, Any]] = []
    for row in rows:
        code = str(row.get("account_code") or "").strip()
        if not code or code.casefold() in current_by_code:
            continue
        parent = nearest_parent(code)
        discovered.append({
            "account_code": code,
            "account_name": str(row.get("source_account_name") or "").strip(),
            "category_id": row.get("category_id"),
            "parent_account_code": parent.get("account_code") if parent else None,
            "parent_account_name": parent.get("account_name") if parent else None,
            "transaction_count": int(row.get("transaction_count") or 0),
            "voucher_count": int(row.get("voucher_count") or 0),
            "first_transaction_date": row.get("first_transaction_date"),
            "last_transaction_date": row.get("last_transaction_date"),
            "is_historical_orphan": True,
            "is_read_only": True,
        })
    return discovered
